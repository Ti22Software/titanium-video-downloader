"""Offline tests: vimeo clip parsing, config resolve, privacy gate, intercept."""

import sys
import types

import pytest
import requests

from titanium_video_downloader.extractors import vimeo as vimeo_mod
from titanium_video_downloader.extractors.base import AuthError
from titanium_video_downloader.extractors.vimeo import _clip_ref, CONFIG_URL, VimeoAuth


@pytest.fixture(autouse=True)
def _fresh_browser():
  """Singleton state must not leak between tests using different fakes."""
  from titanium_video_downloader.core import browser as browser_mod
  browser_mod.close_browser()
  yield
  browser_mod.close_browser()


def test_clip_ref_matrix():
  assert _clip_ref("https://vimeo.com/945678565") == ("945678565", None)
  assert _clip_ref("https://www.vimeo.com/945678565") == ("945678565", None)
  assert _clip_ref("https://vimeo.com/945678565/cb7258092f") == ("945678565", "cb7258092f")
  assert _clip_ref("https://vimeo.com/channels/staffpicks/12345") == ("12345", None)
  assert _clip_ref("https://vimeo.com/groups/shorts/videos/12345") == ("12345", None)
  assert _clip_ref("https://vimeo.com/manage/videos/12345") == ("12345", None)
  assert _clip_ref("https://vimeo.com/video/12345") == ("12345", None)
  assert _clip_ref("https://vimeo.com/") == (None, None)
  assert _clip_ref("not a url") == (None, None)


class _Resp:
  def __init__(self, status=200, payload=None, text=""):
    self.status_code = status
    self._payload = payload
    self.text = text

  def json(self):
    if isinstance(self._payload, Exception):
      raise self._payload
    return self._payload


def _session(payload, status=200):
  class _Stub:
    def get(self, url, headers=None, timeout=None):
      assert "player.vimeo.com/video/945678565/config" in url
      return _Resp(status, payload)
  return _Stub()


PUBLIC = {"video": {"privacy": "anybody", "title": "T", "id": 945678565}}


def test_resolve_public_returns_config_url():
  auth = VimeoAuth()
  url = auth.resolve_embed(_session(PUBLIC), "https://vimeo.com/945678565")
  assert url == CONFIG_URL.format(clip_id="945678565")


def test_resolve_unlisted_preserves_hash():
  auth = VimeoAuth()

  seen = {}

  class _Spy:
    def get(self, url, headers=None, timeout=None):
      seen["url"] = url
      return _Resp(200, PUBLIC)

  url = auth.resolve_embed(_Spy(), "https://vimeo.com/945678565/cb7258092f")
  assert "h=cb7258092f" in url
  assert seen["url"] == url


def test_resolve_private_fails_closed():
  auth = VimeoAuth()
  for privacy in ("nobody", "password", "disable"):
    with pytest.raises(AuthError, match="not public"):
      auth.resolve_embed(_session({"video": {"privacy": privacy}}),
                         "https://vimeo.com/945678565")


def test_resolve_unlisted_privacy_passes_with_hash():
  auth = VimeoAuth()
  url = auth.resolve_embed(
    _session({"video": {"privacy": "unlisted", "title": "U"}}),
    "https://vimeo.com/945678565/cb7258092f")
  assert "h=cb7258092f" in url


def test_resolve_missing_privacy_defaults_public():
  auth = VimeoAuth()
  url = auth.resolve_embed(_session({"video": {"title": "T"}}),
                           "https://vimeo.com/945678565")
  assert url == CONFIG_URL.format(clip_id="945678565")


def test_resolve_transport_failures_exit():
  auth = VimeoAuth()
  with pytest.raises(SystemExit):
    auth.resolve_embed(_session({}, 404), "https://vimeo.com/945678565")
  with pytest.raises(SystemExit):
    auth.resolve_embed(_session(ValueError("nope")), "https://vimeo.com/945678565")
  with pytest.raises(SystemExit):
    auth.resolve_embed(_session(PUBLIC), "https://vimeo.com/")


def test_login_is_noop():
  auth = VimeoAuth()
  sentinel = object()
  assert auth.login(sentinel, None, None) is sentinel
  assert auth.browser_login(sentinel, "a", "b") is sentinel


def test_resolve_falls_back_to_intercept_on_403(monkeypatch):
  calls = []
  monkeypatch.setattr(VimeoAuth, "intercept_config_url",
                      lambda self, session, url, debug=False, headed=False:
                      calls.append((url, headed)) or "INTERCEPTED")
  auth = VimeoAuth()
  url = auth.resolve_embed(_session({}, 403), "https://vimeo.com/945678565")
  assert url == "INTERCEPTED"
  assert calls == [("https://vimeo.com/945678565", False)]


def test_force_intercept_skips_fast_path(monkeypatch):
  def _boom(url, headers=None, timeout=None):
    raise AssertionError("fast path must not run")

  class _Stub:
    def get(self, url, headers=None, timeout=None):
      return _boom(url)

  monkeypatch.setattr(VimeoAuth, "intercept_config_url",
                      lambda self, session, url, debug=False, headed=False:
                      ("URL", url, headed))
  auth = VimeoAuth()
  auth.force_intercept = True
  auth.intercept_headed = True
  assert auth.resolve_embed(_Stub(), "https://vimeo.com/945678565") == \
    ("URL", "https://vimeo.com/945678565", True)


def test_intercept_clicks_play_on_watch_page(monkeypatch):
  """Watch-page Play flow: goto watch URL, role=Play click, config captured."""
  flow = []

  def _fake_pw(config_url):
    mod = types.ModuleType("playwright.sync_api")
    mod.TimeoutError = _FakeTimeout

    class _Locator:
      def __init__(self, page):
        self._page = page

      @property
      def first(self):
        return self

      def wait_for(self, state=None, timeout=None):
        flow.append("wait")

      def scroll_into_view_if_needed(self, timeout=None):
        flow.append("scroll")

      def click(self, timeout=None):
        flow.append("click")
        for handler in self._page._handlers.get("request", []):
          handler(type("Req", (), {"url": config_url})())

    class _Page:
      def __init__(self):
        self._handlers = {}

      def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

      def goto(self, url, wait_until=None, timeout=None):
        flow.append(("goto", url))

      def expect_response(self, pred, timeout=None):
        page = self

        class _Ctx:
          def __enter__(self):
            return self

          def __exit__(self, *exc):
            return False

          @property
          def value(self):
            return _FakeResp(config_url, 200)

        return _Ctx()

      def get_by_role(self, role, name=None):
        flow.append(("role", role, name))
        return _Locator(self)

      def content(self):
        return "<html>watch</html>"

    class _Ctx2:
      def new_page(self):
        return _Page()

      def cookies(self):
        return []

      def close(self):
        pass

    class _Browser:
      def new_context(self, **kw):
        return _Ctx2()

      def is_connected(self):
        return True

      def close(self):
        pass

    class _Chromium:
      @staticmethod
      def launch(headless=True):
        return _Browser()

    class _Pw:
      chromium = _Chromium()

    class _Sync:
      def start(self):
        return _Pw()

      def stop(self):
        pass

    mod.sync_playwright = lambda: _Sync()
    return mod

  monkeypatch.setitem(sys.modules, "playwright.sync_api", _fake_pw(CFG))
  monkeypatch.setattr(vimeo_mod, "_fetch_config_json",
                      lambda session, url: {"video": {"privacy": "anybody"}})
  auth = VimeoAuth()
  url = auth.intercept_config_url(requests.Session(), "https://vimeo.com/945678565")
  assert url == CFG
  assert ("goto", "https://vimeo.com/945678565") in flow
  assert ("role", "button", "Play") in flow
  assert "wait" in flow and "scroll" in flow and "click" in flow


class _FakeTimeout(Exception):
  pass


class _FakeResp:
  def __init__(self, url, status=200):
    self.url = url
    self.status = status


def _fake_playwright(config_url=None, status=200, fire_listener=True,
                     do_timeout=False, seen_headed=None):
  """Stub playwright.sync_api: scripted player page (no real browser)."""
  if seen_headed is None:
    seen_headed = []
  mod = types.ModuleType("playwright.sync_api")
  mod.TimeoutError = _FakeTimeout

  class _Page:
    def __init__(self):
      self._handlers = {}

    def on(self, event, handler):
      self._handlers[event] = handler

    def goto(self, url, wait_until=None, timeout=None):
      if fire_listener and config_url and "request" in self._handlers:
        self._handlers["request"](type("Req", (), {"url": config_url})())

    def get_by_role(self, role, name=None):
      class _Locator:
        @property
        def first(self):
          return self

        def wait_for(self, state=None, timeout=None):
          pass

        def scroll_into_view_if_needed(self, timeout=None):
          pass

        def click(self, timeout=None):
          pass

      return _Locator()

    def expect_response(self, pred, timeout=None):
      page = self

      class _Ctx:
        def __enter__(self):
          return self

        def __exit__(self, *exc):
          if do_timeout:
            raise _FakeTimeout()
          return False

        @property
        def value(self):
          return _FakeResp(page._config_url, page._status)

      _Ctx._page = page
      ctx = _Ctx()
      ctx._page._config_url = config_url
      ctx._page._status = status
      return ctx

    def content(self):
      return "<html>player</html>"

  class _Ctx2:
    def __init__(self):
      self.page = _Page()

    def new_page(self):
      return self.page

    def cookies(self):
      return [{"name": "vuid", "value": "stub", "domain": ".vimeo.com", "path": "/"}]

    def close(self):
      pass

  class _Browser:
    def __init__(self, ctx):
      self._ctx = ctx

    def is_connected(self):
      return True

    def new_context(self, **kw):
      return self._ctx

    def close(self):
      pass

  ctx_obj = _Ctx2()

  class _Chromium:
    @staticmethod
    def launch(headless=True):
      seen_headed.append(headless)
      return _Browser(ctx_obj)

  class _Pw:
    chromium = _Chromium()

    def stop(self):
      pass

  class _Sync:
    def start(self):
      return _Pw()

  mod.sync_playwright = lambda: _Sync()
  return mod


CFG = "https://player.vimeo.com/video/945678565/config?h=abc&s=sig_exp"


def test_intercept_captures_player_config(monkeypatch):
  monkeypatch.setitem(sys.modules, "playwright.sync_api", _fake_playwright(CFG))
  monkeypatch.setattr(vimeo_mod, "_fetch_config_json",
                      lambda session, url: {"video": {"privacy": "anybody"}})
  auth = VimeoAuth()
  session = requests.Session()
  url = auth.intercept_config_url(session, "https://player.vimeo.com/video/945678565")
  assert url == CFG
  assert session.cookies.get("vuid", domain=".vimeo.com") == "stub"


def test_intercept_headed_passthrough(monkeypatch):
  seen = []
  monkeypatch.setitem(sys.modules, "playwright.sync_api", _fake_playwright(CFG, seen_headed=seen))
  monkeypatch.setattr(vimeo_mod, "_fetch_config_json",
                      lambda session, url: {"video": {"privacy": "anybody"}})
  auth = VimeoAuth()
  auth.intercept_config_url(requests.Session(),
                            "https://player.vimeo.com/video/945678565", headed=True)
  assert seen == [False]


def test_intercept_timeout_without_requests_fails(monkeypatch):
  monkeypatch.setitem(sys.modules, "playwright.sync_api",
                      _fake_playwright(None, do_timeout=True))
  auth = VimeoAuth()
  with pytest.raises(AuthError, match="did not request"):
    auth.intercept_config_url(requests.Session(),
                              "https://player.vimeo.com/video/945678565")


def test_intercept_rejected_status_fails(monkeypatch):
  monkeypatch.setitem(sys.modules, "playwright.sync_api",
                      _fake_playwright(CFG, status=403))
  auth = VimeoAuth()
  with pytest.raises(AuthError, match="rejected"):
    auth.intercept_config_url(requests.Session(),
                              "https://player.vimeo.com/video/945678565")


def test_resolve_falls_back_to_intercept_on_403(monkeypatch):
  calls = []
  monkeypatch.setattr(VimeoAuth, "intercept_config_url",
                      lambda self, session, url, debug=False, headed=False:
                      calls.append((url, headed)) or "INTERCEPTED")
  auth = VimeoAuth()
  url = auth.resolve_embed(_session({}, 403), "https://vimeo.com/945678565")
  assert url == "INTERCEPTED"
  assert calls == [("https://vimeo.com/945678565", False)]


def test_force_intercept_skips_fast_path(monkeypatch):
  def _boom(url, headers=None, timeout=None):
    raise AssertionError("fast path must not run")

  class _Stub:
    def get(self, url, headers=None, timeout=None):
      return _boom(url)

  monkeypatch.setattr(VimeoAuth, "intercept_config_url",
                      lambda self, session, url, debug=False, headed=False:
                      ("URL", url, headed))
  auth = VimeoAuth()
  auth.force_intercept = True
  auth.intercept_headed = True
  assert auth.resolve_embed(_Stub(), "https://vimeo.com/945678565") == \
    ("URL", "https://vimeo.com/945678565", True)
