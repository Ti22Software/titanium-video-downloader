"""StudyGateway (VHX OTT) extractor: SAML login + embed/config resolve.

Live-verified: browser SAML login → tokenized embed → 60s config →
playlist → segments → mux (ffprobe/VLC clean, md5-deterministic).
"""

import json
import re
import sys
import time

import requests

from ..core.fetcher import fetch_text
from ..core.session import (
  TIMEOUT,
  UA,
  WATCH_BROWSE_URL,
  WATCH_DOC_HEADERS,
  WATCH_LOGIN_URL,
)
from .base import (
  AuthError,
  AuthProvider,
  _auth_state,
  _debug_auth_state,
  _debug_redact_url,
  _host,
  fail,
)


# Post-submit outcome polling: fail fast on a visible rejection while the
# slow-good path (Cloudflare challenge + SAML auto-post) keeps the full
# backstop. Bad creds 302 back to a login?SAMLRequest=… page rendering a
# bare `div.error-message`-style container; good creds leave for
# saml/consume → browse?ticket=. Uncertain states always resolve toward
# waiting (today's floor) — only positive failure evidence fails fast.
_LOGIN_SETTLE_S = 10
_LOGIN_POLL_S = 1
_LOGIN_BACKSTOP_S = 90


def _login_error_text(page):
  """Visible login-rejection text, or None. Never raises."""
  try:
    loc = page.locator('div[class*="error" i]')
    try:
      n = loc.count()
    except Exception:
      return None
    for i in range(min(n, 5)):
      try:
        item = loc.nth(i)
        if not item.is_visible():
          continue
        text = (item.inner_text() or "").strip()
      except Exception:
        continue
      if text and re.search(r"credential|password|match|invalid|incorrect",
                            text, re.IGNORECASE):
        # Deliberately narrow (no bare "login"/"sign in"): an
        # informational div must never fail a good login. Anything
        # unmatched falls back to the full backstop wait.
        return text[:160]
  except Exception:
    pass
  return None


def _wait_for_login_outcome(page):
  """Poll post-submit state to the backstop. Returns on browse URL;
  raises AuthError on visible rejection or, as before, on timeout."""
  start = time.monotonic()
  while True:
    el = time.monotonic() - start
    try:
      url = page.url or ""
    except Exception:
      url = ""
    if "/browse" in url:
      return
    if el >= _LOGIN_SETTLE_S and "/login" in url:
      err = _login_error_text(page)
      if err:
        raise AuthError("browser login rejected — bad email/password "
                        f"(site says: {err!r}).")
    if el >= _LOGIN_BACKSTOP_S:
      break
    time.sleep(min(_LOGIN_POLL_S, max(0.1, _LOGIN_BACKSTOP_S - el)))
  try:
    cur = page.url or ""
  except Exception:
    cur = ""
  if "/login" in cur:
    raise AuthError("browser login timed out staying on the login page "
                    "(check creds / 2FA / CAPTCHA; retry with --headed to watch).")
  raise AuthError(f"browser login timed out at {_debug_redact_url(cur)} — "
                  "site/Cloudflare slow? Retry, or add --headed to watch.")


class StudyGatewayAuth(AuthProvider):
  """StudyGateway (VHX OTT): browser SAML login + tokenized embed resolve."""

  site_key = "studygateway"
  requires_auth = True
  cookie_domains = ("studygateway.com",)

  def match(self, url):
    return _host(url) == "watch.studygateway.com"

  def login(self, session, email, password, debug=False):
    # Pure-requests SAML login was removed: the server demands a real
    # reCAPTCHA v3 token no script can mint, so this path could only ever
    # fail (and once printed false "login ok"). Logins always harvest via
    # browser (see cli) — or use --cookies.
    raise AuthError("requests login is not supported for studygateway — "
                    "logins harvest via browser automatically, or use --cookies.")

  def browser_login(self, session, email, password, debug=False, headed=False):
    """Playwright harvester: real Chromium passes reCAPTCHA v3 natively,
    follows saml/consume → browse?ticket=, exports cookies into session."""
    try:
      from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
      raise AuthError("playwright not installed — run: pip install playwright "
                      "&& playwright install chromium, or use --cookies.")
    if not email or not password:
      raise AuthError("browser login needs --email/--password or "
                      "TI22_VIDEO_DL_STUDYGATEWAY_EMAIL/PASSWORD.")
    from ..core.browser import get_browser
    browser = get_browser(headed)
    ctx = browser.new_context(user_agent=UA)
    page = ctx.new_page()
    try:
      try:
        page.goto(WATCH_LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_url("**/login**", timeout=30000)
      except PwTimeout:
        raise AuthError("browser login timed out reaching the login page "
                        "(site/Cloudflare slow?) — retry, or add --headed to watch.")
      www_url = page.url
      if debug:
        print(f"debug-login: browser at {_debug_redact_url(www_url)} "
              f"has_saml={'SAMLRequest=' in www_url}", file=sys.stderr)
      try:
        page.fill('input[type="email"], input[name="email"]', email, timeout=15000)
        page.fill('input[type="password"], input[name="password"]', password, timeout=15000)
        page.click('button[type="submit"], input[type="submit"]', timeout=15000)
      except PwTimeout:
        raise AuthError("browser login could not fill/submit the login form "
                        "(page format changed?) — retry with --headed to watch.")
      # Do NOT use expect_navigation: wrong creds / slow Cloudflare / JS
      # SAML auto-post may never fire a navigation (previously a raw
      # TimeoutError traceback). Poll for the browse URL instead, failing
      # fast when the page settles back on login with a visible rejection.
      # Hint stays generic on purpose — no chasing site copy changes.
      _wait_for_login_outcome(page)
      html = page.content()
      st = _auth_state(html)
      _debug_auth_state("browser browse auth", html, debug)
      if not st.get("authed"):
        raise AuthError("browser login did not reach authenticated browse "
                        "(check creds / 2FA / CAPTCHA in headed mode with --headed).")
      for c in ctx.cookies():
        try:
          session.cookies.set(c["name"], c["value"],
                              domain=c.get("domain", ""),
                              path=c.get("path", "/"))
        except Exception:
          pass
      if debug:
        names = sorted({c.name for c in session.cookies})
        print(f"debug-login: browser harvest cookies={names}", file=sys.stderr)
    finally:
      try:
        ctx.close()
      except Exception:
        pass
    return session

  def resolve_embed(self, session, watch_url, debug=False):
    # Browsers send the LINKING page as Referer, never self. Self-referer
    # renders the generic player variant without auth-user-token, so use
    # /browse like a browse→click navigation.
    headers = dict(WATCH_DOC_HEADERS)
    headers["Referer"] = WATCH_BROWSE_URL
    try:
      r = session.get(watch_url, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as e:
      fail(f"could not fetch watch page: {e}")
    if r.status_code in (401, 403):
      fail("watch page rejected — not logged in (stale cookies? re-export "
           "a fresh cookies file). Pass --email/--password or --cookies.")
    if r.status_code != 200:
      fail(f"watch page returned HTTP {r.status_code}.")
    html = r.text
    unesc = html.replace("&amp;", "&")
    raw = re.findall(r'https://embed\.vhx\.tv/videos/\d+\?[^"\'\s<>\\]+', unesc)
    frames = re.findall(r'<iframe[^>]+src="([^"]*embed\.vhx\.tv[^"]*)"',
                        html, re.IGNORECASE | re.DOTALL)
    cfg = re.findall(r'embed_url["\']?\s*:\s*["\']([^"\']*embed\.vhx\.tv[^"\']*)["\']',
                     unesc)
    bare_tok = re.search(
      r'["\']auth[-_]user[-_]token["\']?\s*[:=]\s*["\']([^"\']{20,})["\']', html)
    bare = bare_tok.group(1) if bare_tok else ""
    low = html.lower()
    iframe_count = len(re.findall(r'<iframe', html, re.IGNORECASE))
    embed_var = "embed_url" in low and "embed.vhx.tv" in low
    auth_markers = sum(low.count(m) for m in
                       ("auth-user-token", "auth_user_token", "user_auth_token"))
    has_window_token = "window.token" in low
    seen = set()
    cands = []
    for u in raw + frames + cfg:
      u = u.replace("&amp;", "&")
      if u not in seen:
        seen.add(u)
        cands.append(u)
    tok = [u for u in cands
           if "auth-user-token" in u.lower() or "auth_user_token" in u.lower()]
    if debug:
      print(f"debug-login: watch page len={len(html)} referer={_debug_redact_url(headers['Referer'])} "
            f"embed_candidates={len(cands)} tokenized={len(tok)} "
            f"lengths={[len(u) for u in cands[:5]]} iframe_count={iframe_count} "
            f"embed_url_var={embed_var} auth_markers={auth_markers} "
            f"window_TOKEN={has_window_token} bare_token={bool(bare)}",
            file=sys.stderr)
    if tok:
      tok.sort(key=len, reverse=True)
      return tok[0]
    if bare and cands:
      # Token held separately from base URL (e.g. JS var) — join to longest base.
      cands.sort(key=len, reverse=True)
      base = cands[0]
      sep = "&" if "?" in base else "?"
      return f"{base}{sep}auth-user-token={bare}"
    if cands:
      # No tokenized URL found — the generic iframe 401s on fetch. Report it
      # so the user can paste slug-page embed markers for the next fix.
      print("note: no auth-user-token in slug embed URLs — trying longest candidate; "
            "expect embed 401/403 if the site moved the token",
            file=sys.stderr)
      cands.sort(key=len, reverse=True)
      return cands[0]
    fail("could not find embed.vhx.tv iframe URL on watch page "
         "(login expired or page format changed).")


def extract_ottdata(embed_html):
  """Return (config_url, title, video_id) from embed.vhx.tv document HTML."""
  marker = "window.OTTData"
  i = embed_html.find(marker)
  if i < 0:
    return None, None, None
  eq = embed_html.find("=", i)
  end = embed_html.find("</script>", eq)
  if eq < 0 or end < 0:
    return None, None, None
  blob = embed_html[eq + 1:end].strip().rstrip(";")
  try:
    data = json.loads(blob)
  except ValueError:
    return None, None, None
  video = data.get("video") or {}
  return data.get("config_url"), video.get("title"), video.get("id")


def pick_playlist_url(config):
  """Return (primary_url, fallback_url, quality_map) from player config JSON."""
  try:
    dash = config["request"]["files"]["dash"]
    cdns = dash["cdns"]
    default = dash.get("default_cdn")
  except (KeyError, TypeError):
    fail("config JSON has no request.files.dash — unexpected player response.")
  if default not in cdns:
    default = next(iter(cdns))
  ordered = [default] + [k for k in cdns if k != default]
  urls = []
  for k in ordered:
    cdn = cdns[k] or {}
    urls.append(cdn.get("avc_url") or cdn.get("url"))
  primary = urls[0] if len(urls) > 0 else None
  fallback = urls[1] if len(urls) > 1 else None
  if not primary:
    fail("config JSON has no dash CDN URL.")
  qmap = {}
  for s in dash.get("streams_avc") or dash.get("streams") or []:
    if s.get("id") and s.get("quality"):
      qmap[s["id"]] = s["quality"]
  return primary, fallback, qmap


def resolve_config_url(session, input_url):
  host = _host(input_url)
  if host == "player.vimeo.com" and "/config" in (input_url or ""):
    return input_url, None, None
  if host == "embed.vhx.tv":
    html = fetch_text(session, input_url)
    config_url, title, vid = extract_ottdata(html)
    if not config_url:
      fail("could not find window.OTTData.config_url in embed page "
           "(token expired or page format changed).")
    return config_url, title, vid
  if host == "watch.studygateway.com":
    fail("watch URL needs login first — this path should have been resolved "
         "to an embed URL before resolve_config_url.")
  fail("input must be a watch.studygateway.com URL, an embed.vhx.tv URL, "
       "a player.vimeo.com config URL, a vimeo.com video URL, or a "
       "rumble.com watch (/v…), embed (/embed/), or shorts (/shorts/) URL.")
