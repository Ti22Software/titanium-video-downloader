"""Public Vimeo extractor (Phase 2a): player-config resolve, no-auth first.

Public player-config JSON has the same ``request.files.dash`` shape as the
StudyGateway path, so everything downstream (playlist → segments → mux,
quality selection/listing) is shared verbatim. ``resolve_embed`` returns
the player *config* URL itself — ``cli`` already passes those straight
through to the shared pipeline.

Two resolve paths, in order:

1. Fast path (plain ``requests``): bare ``/config[?h=]``. Works for lax
   videos, costs one cheap round-trip when it misses.
2. Browser intercept: headless Chromium loads the watch page, clicks Play,
   and captures the player's *own* ``/config`` request with its minted
   ``h=``/``s=`` params. Triggered automatically on fast-path 401/403/410,
   or forced via ``force_intercept`` (wired to ``--use-browser-login`` by
   cli). Verified live: Play click on the watch page fires config.

Private / password / embed-restricted videos fail closed on the
``video.privacy`` gate (``anybody``/``unlisted`` pass).
"""

import re
import sys

import requests

from ..core.session import TIMEOUT, UA
from .base import AuthError, AuthProvider, _debug_redact_url, _host, fail

CONFIG_URL = "https://player.vimeo.com/video/{clip_id}/config"

VIMEO_HEADERS = {
  "User-Agent": UA,
  "Accept": "application/json, */*",
  "Accept-Language": "en-US,en;q=0.5",
}

_CLIP_RE = re.compile(
  r"vimeo\.com/(?:channels/[^/?#]+/|groups/[^/?#]+/videos/"
  r"|manage/videos/|video/)?(\d+)(?:/([a-f0-9]+))?",
  re.IGNORECASE,
)


def _clip_ref(url):
  """Return (clip_id, unlisted_hash|None) from a vimeo watch URL."""
  m = _CLIP_RE.search(url or "")
  if not m:
    return None, None
  return m.group(1), m.group(2)


def _gate_privacy(data):
  """Fail closed on non-public videos. Returns the video dict."""
  video = data.get("video") or {}
  privacy = video.get("privacy", "anybody")
  # Unlisted rides through with its h= hash (no-hash unlisted 401/403s
  # at fetch); everything else non-public fails closed. Private Vimeo
  # login is deferred past Phase 2a.
  if privacy not in ("anybody", "unlisted"):
    raise AuthError(f"vimeo video is not public (privacy={privacy}) — "
                    "private/password login is deferred past Phase 2a.")
  return video


def _fetch_config_json(session, config_url):
  """GET a config URL as JSON (transport errors fail, redacted URL)."""
  try:
    r = session.get(config_url, headers=VIMEO_HEADERS, timeout=TIMEOUT)
  except requests.RequestException as e:
    fail(f"could not fetch vimeo config: {e}")
  if r.status_code != 200:
    fail(f"vimeo config returned HTTP {r.status_code}.")
  try:
    return r.json()
  except ValueError:
    fail("vimeo config did not return JSON.")


class VimeoAuth(AuthProvider):
  """Public Vimeo: match real, login is a no-op, resolve via player config."""

  site_key = "vimeo"
  requires_auth = False

  # Set by cli when --use-browser-login targets vimeo: skip the fast path,
  # headedness follows --headed. Documented hook, not ABC surface.
  force_intercept = False
  intercept_headed = False

  def match(self, url):
    # Watch pages only: player.vimeo.com/config URLs are a downstream
    # artifact handled by resolve_config_url, and must bypass providers.
    return _host(url) in ("vimeo.com", "www.vimeo.com")

  def login(self, session, email, password, debug=False):
    return session

  def browser_login(self, session, email, password, debug=False, headed=False):
    return session

  def intercept_config_url(self, session, watch_url, debug=False, headed=False):
    """Load the WATCH page headless, click Play, capture /config.

    The player only initializes inside its embedding context (a bare
    player page renders an idle shell that never fetches config), so the
    watch page drives playback and we intercept the player's own config
    request — exactly what every real embed does. Exported player cookies
    join the session for the shared downstream pipeline.
    """
    try:
      from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
      raise AuthError("playwright not installed — run: pip install playwright "
                      "&& playwright install chromium, or use --cookies.")
    with sync_playwright() as pw:
      browser = pw.chromium.launch(headless=not headed)
      ctx = browser.new_context(viewport={"width": 1280, "height": 800},
                                user_agent=UA)
      page = ctx.new_page()
      seen = []

      def _on_request(req):
        url = req.url
        if "player.vimeo.com" in url and "/config" in url:
          seen.append(url)

      page.on("request", _on_request)
      try:
        try:
          page.goto(watch_url, wait_until="domcontentloaded", timeout=45000)
          play = page.get_by_role("button", name="Play").first
          play.wait_for(state="visible", timeout=30000)
          play.scroll_into_view_if_needed(timeout=10000)
        except PwTimeout:
          raise AuthError("vimeo Play button never appeared "
                          "(page format changed?) — retry with --headed to watch.")
        try:
          with page.expect_response(
              lambda resp: "player.vimeo.com" in resp.url and "/config" in resp.url,
              timeout=45000) as resp_info:
            play.click(timeout=15000)
          resp = resp_info.value
          config_url = resp.url
          status = resp.status
        except PwTimeout:
          if not seen:
            raise AuthError("vimeo player did not request its config after Play "
                            "(site slow?) — retry with --headed to watch.")
          config_url = seen[0]
          status = 200
        if debug:
          print(f"debug-login: vimeo intercept status={status} "
                f"url={_debug_redact_url(config_url)}", file=sys.stderr)
        for c in ctx.cookies():
          try:
            session.cookies.set(c["name"], c["value"],
                                domain=c.get("domain", ""),
                                path=c.get("path", "/"))
          except Exception:
            pass
        if debug:
          try:
            names = sorted({c.name for c in session.cookies})
          except Exception:
            names = []
          print(f"debug-login: vimeo harvest cookies={names}", file=sys.stderr)
      finally:
        try:
          ctx.close()
        except Exception:
          pass
        try:
          browser.close()
        except Exception:
          pass
    if status != 200:
      raise AuthError(f"vimeo player config rejected (HTTP {status}) — private, "
                      "password-protected, or embed-restricted video?")
    _gate_privacy(_fetch_config_json(session, config_url))
    return config_url

  def resolve_embed(self, session, watch_url, debug=False):
    clip_id, unlisted_hash = _clip_ref(watch_url)
    if not clip_id:
      fail(f"could not parse vimeo clip id from URL: {_debug_redact_url(watch_url)}")
    if self.force_intercept:
      if debug:
        print("debug-login: vimeo force_intercept (--use-browser-login), "
              "skipping fast path", file=sys.stderr)
      return self.intercept_config_url(session, watch_url,
                                       debug=debug, headed=self.intercept_headed)
    config_url = CONFIG_URL.format(clip_id=clip_id)
    if unlisted_hash:
      config_url += f"?h={unlisted_hash}"
    headers = dict(VIMEO_HEADERS)
    headers["Referer"] = "https://vimeo.com/"
    headers["Origin"] = "https://vimeo.com"
    try:
      r = session.get(config_url, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as e:
      fail(f"could not fetch vimeo config: {e}")
    if r.status_code in (401, 403, 410):
      if debug:
        print(f"debug-login: vimeo fast path HTTP {r.status_code}, "
              "falling back to browser intercept", file=sys.stderr)
      return self.intercept_config_url(session, watch_url,
                                       debug=debug, headed=self.intercept_headed)
    if r.status_code != 200:
      fail(f"vimeo config returned HTTP {r.status_code}.")
    try:
      data = r.json()
    except ValueError:
      fail("vimeo config did not return JSON.")
    video = _gate_privacy(data)
    if debug:
      print(f"debug-login: vimeo clip={clip_id} "
            f"unlisted={bool(unlisted_hash)} privacy={video.get('privacy', 'anybody')} "
            "resolve=fast-path", file=sys.stderr)
    return config_url
