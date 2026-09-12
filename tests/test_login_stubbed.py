"""Offline tests: requests login removed; browser guard rails."""

import sys
import time

import pytest

from titanium_video_downloader.extractors.base import AuthError
from titanium_video_downloader.extractors import studygateway as sg_mod
from titanium_video_downloader.extractors.studygateway import StudyGatewayAuth


def test_requests_login_removed():
  auth = StudyGatewayAuth()
  with pytest.raises(AuthError, match="not supported"):
    auth.login(object(), "a@b", "pw")


def test_browser_login_needs_playwright(monkeypatch):
  monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
  auth = StudyGatewayAuth()
  with pytest.raises(AuthError, match="playwright not installed"):
    auth.browser_login(object(), "a@b", "pw")


def test_browser_login_needs_creds():
  auth = StudyGatewayAuth()
  with pytest.raises(AuthError, match="needs"):
    auth.browser_login(object(), None, None)


LOGIN = "https://www.studygateway.com/login?SAMLRequest=x"
BROWSE = "https://watch.studygateway.com/browse"
BAD_TEXT = "These credentials do not match our records."


class _FakeItem:
  def __init__(self, text):
    self._text = text

  def is_visible(self):
    return True

  def inner_text(self):
    return self._text


class _FakeLoc:
  def __init__(self, texts=None, explode=False):
    self._texts = texts or []
    self._explode = explode

  def count(self):
    if self._explode:
      raise RuntimeError("boom")
    return len(self._texts)

  def nth(self, i):
    return _FakeItem(self._texts[i])


class _FakePage:
  """Scripted (url, [error texts]) states, one advance per sleep tick."""

  def __init__(self, states, explode_loc=False):
    self._states = states
    self._explode = explode_loc
    self._i = 0

  @property
  def url(self):
    return self._states[min(self._i, len(self._states) - 1)][0]

  def advance(self):
    self._i += 1

  def locator(self, _sel):
    if self._explode:
      return _FakeLoc(explode=True)
    return _FakeLoc(self._states[min(self._i, len(self._states) - 1)][1])


class _Clock:
  def __init__(self, page):
    self.t = 0.0
    self.page = page

  def monotonic(self):
    return self.t

  def sleep(self, s):
    self.t += s
    self.page.advance()


def _run(page, monkeypatch):
  clock = _Clock(page)
  monkeypatch.setattr(time, "monotonic", clock.monotonic)
  monkeypatch.setattr(time, "sleep", clock.sleep)
  return clock


def test_login_outcome_fast_rejection(monkeypatch):
  states = [(LOGIN, [])] * 10 + [(LOGIN, [BAD_TEXT])]
  page = _FakePage(states)
  clock = _run(page, monkeypatch)
  with pytest.raises(AuthError, match="rejected — bad email/password"):
    sg_mod._wait_for_login_outcome(page)
  assert clock.t < 20  # seconds, not the 90s backstop


def test_login_outcome_slow_good(monkeypatch):
  states = [(LOGIN, [])] * 25 + [(BROWSE, [])]
  page = _FakePage(states)
  clock = _run(page, monkeypatch)
  assert sg_mod._wait_for_login_outcome(page) is None
  assert clock.t >= 25


def test_login_outcome_backstop_markerless(monkeypatch):
  page = _FakePage([(LOGIN, [])])
  clock = _run(page, monkeypatch)
  with pytest.raises(AuthError, match="staying on the login page"):
    sg_mod._wait_for_login_outcome(page)
  assert clock.t == sg_mod._LOGIN_BACKSTOP_S


def test_login_outcome_backstop_elsewhere(monkeypatch):
  page = _FakePage([("https://www.studygateway.com/challenge", [])])
  _run(page, monkeypatch)
  with pytest.raises(AuthError, match="timed out at"):
    sg_mod._wait_for_login_outcome(page)


def test_login_outcome_detector_exceptions_wait(monkeypatch):
  page = _FakePage([(LOGIN, [])], explode_loc=True)
  clock = _run(page, monkeypatch)
  with pytest.raises(AuthError, match="staying on the login page"):
    sg_mod._wait_for_login_outcome(page)
  assert clock.t == sg_mod._LOGIN_BACKSTOP_S


def test_login_error_ignores_unrelated_text(monkeypatch):
  page = _FakePage([(LOGIN, ["Error loading the captcha widget."])])
  clock = _run(page, monkeypatch)
  with pytest.raises(AuthError, match="staying on the login page"):
    sg_mod._wait_for_login_outcome(page)
  assert clock.t == sg_mod._LOGIN_BACKSTOP_S
