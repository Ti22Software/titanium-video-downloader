"""StudyGateway (VHX OTT) extractor: SAML login + embed/config resolve.

Live-verified: browser SAML login → tokenized embed → 60s config →
playlist → segments → mux (ffprobe/VLC clean, md5-deterministic).
"""

import json
import re
import sys
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from ..core.fetcher import fetch_text
from ..core.session import (
  SAML_POST_URL,
  TIMEOUT,
  UA,
  WATCH_BROWSE_URL,
  WATCH_DOC_HEADERS,
  WATCH_LOGIN_URL,
  WWW_BASE,
  WWW_DOC_HEADERS,
  WWW_LOGIN_URL,
)
from .base import (
  AuthError,
  AuthProvider,
  _auth_state,
  _debug_auth_state,
  _debug_login_state,
  _debug_redact_url,
  _hidden_input,
  _host,
  _looks_like_challenge,
  fail,
)


class StudyGatewayAuth(AuthProvider):
  """Happy-path SAML form login for studygateway (requests-only, no JS)."""

  site_key = "studygateway"
  requires_auth = True

  def match(self, url):
    return _host(url) == "watch.studygateway.com"

  def login(self, session, email, password, debug=False):
    if not email or not password:
      raise AuthError("watch URL needs --email/--password or TI22_EMAIL/TI22_PASSWORD.")
    # 1. GET watch/login WITHOUT following redirects to capture SAMLRequest.
    # Verified: 302 Location: https://www.studygateway.com/login?SAMLRequest=...
    saml_request = ""
    relay_state = ""
    token = ""
    www_login_url = WWW_LOGIN_URL
    try:
      w0 = session.get(WATCH_LOGIN_URL, headers=WATCH_DOC_HEADERS,
                       timeout=TIMEOUT, allow_redirects=False)
      if debug:
        loc = w0.headers.get("Location", "") if hasattr(w0, "headers") else ""
        print(f"debug-login: GET watch/login (no-follow) status={w0.status_code} "
              f"location={_debug_redact_url(loc)} has_saml={'SAMLRequest=' in loc}",
              file=sys.stderr)
      if w0.status_code in (301, 302, 303, 307, 308):
        loc = w0.headers.get("Location", "")
        if loc:
          www_login_url = urljoin(WATCH_LOGIN_URL, loc)
          try:
            qs = parse_qs(urlparse(www_login_url).query)
            if "SAMLRequest" in qs and qs["SAMLRequest"]:
              saml_request = qs["SAMLRequest"][0]
            if "RelayState" in qs and qs["RelayState"]:
              relay_state = qs["RelayState"][0]
          except Exception:
            pass
      else:
        # Unexpected 200: try hidden inputs, then fall back to followed GET below.
        try:
          saml_request = _hidden_input(w0.text, "SAMLRequest") or ""
          relay_state = _hidden_input(w0.text, "RelayState") or ""
        except Exception:
          pass
      # Still seed cookies with a followed GET (harmless, keeps _session fresh).
      try:
        session.get(WATCH_LOGIN_URL, headers=WATCH_DOC_HEADERS, timeout=TIMEOUT)
      except requests.RequestException:
        pass
    except requests.RequestException as e:
      raise AuthError(f"could not reach watch login: {e}")
    # 2. GET the full www login URL INCLUDING ?SAMLRequest=... (not bare /login).
    try:
      r = session.get(www_login_url, headers=WWW_DOC_HEADERS, timeout=TIMEOUT)
    except requests.RequestException as e:
      raise AuthError(f"could not reach www login: {e}")
    if r.status_code != 200:
      raise AuthError(f"www login returned HTTP {r.status_code}.")
    html = r.text
    www_login_url = r.url  # may now include ?SAMLRequest=...
    if "SAMLRequest=" in www_login_url:
      m = re.search(r"SAMLRequest=([^&]+)", www_login_url)
      if m:
        saml_request = m.group(1)
    token = _hidden_input(html, "_token") or ""
    saml_request = _hidden_input(html, "SAMLRequest") or saml_request
    relay_state = _hidden_input(html, "RelayState") or relay_state
    if debug:
      print(f"debug-login: www login url={_debug_redact_url(www_login_url)} "
            f"has_token={bool(token)} token_len={len(token)} "
            f"has_samlrequest={bool(saml_request)} saml_len={len(saml_request)} "
            f"has_relay={bool(relay_state)}",
            file=sys.stderr)
      _debug_login_state("www login html", html=html, url=www_login_url,
                         status=r.status_code, session=session)
    if not token or not saml_request:
      if _looks_like_challenge(html):
        raise AuthError("login blocked by bot check (challenge page) — "
                        "retry with --cookies cookies.txt (Netscape export).")
      raise AuthError("could not parse www login form (page format changed). "
                      "Re-run with --debug-login and report has_token/has_samlrequest.")
    # 3. POST creds to SAML endpoint (happy path: no JS recaptcha token).
    form = {
      "_token": token,
      "login-type": "original",
      "email": email,
      "password": password,
      "SAMLRequest": saml_request,
      "RelayState": relay_state,
      "g-recaptcha-response": "",
    }
    headers = dict(WWW_DOC_HEADERS)
    headers["Origin"] = WWW_BASE
    headers["Referer"] = www_login_url
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
      r = session.post(SAML_POST_URL, data=form, headers=headers,
                       timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
      raise AuthError(f"login POST failed: {e}")
    if debug:
      _debug_login_state("POST login/saml", html=getattr(r, "text", ""),
                         url=getattr(r, "url", ""), status=r.status_code,
                         session=session)
    # 4. Verify AUTHORITATIVELY: /browse is PUBLIC (200 logged-out), so
    # status/url prove nothing. Require _current_user/logout markers.
    try:
      b = session.get(WATCH_BROWSE_URL, headers=WATCH_DOC_HEADERS,
                      timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
      raise AuthError(f"login verify failed: {e}")
    if debug:
      _debug_login_state("GET watch/browse", html=getattr(b, "text", ""),
                         url=getattr(b, "url", ""), status=b.status_code,
                         session=session)
    st = _auth_state(getattr(b, "text", ""))
    _debug_auth_state("browse auth", getattr(b, "text", ""), debug)
    if not st.get("authed"):
      if b.status_code != 200 or "/login" in (b.url or ""):
        body = b.text.lower() if getattr(b, "text", None) else ""
        if _looks_like_challenge(b.text if getattr(b, "text", None) else ""):
          raise AuthError("login blocked by bot check (challenge on verify) — "
                          "retry with --cookies cookies.txt (Netscape export).")
      raise AuthError("login rejected (server kept logged-out browse page — "
                      "bad email/password, expired SAMLRequest, or missing "
                      "reCAPTCHA token) — retry with --cookies cookies.txt or "
                      "--use-browser-login if creds are correct.")
    return session

  def browser_login(self, session, email, password, debug=False, headed=False):
    """Playwright harvester: real Chromium passes reCAPTCHA v3 natively,
    follows saml/consume → browse?ticket=, exports cookies into session."""
    try:
      from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
      raise AuthError("playwright not installed — run: pip install playwright "
                      "&& playwright install chromium, or use --cookies.")
    if not email or not password:
      raise AuthError("browser login needs --email/--password or TI22_EMAIL/TI22_PASSWORD.")
    with sync_playwright() as pw:
      browser = pw.chromium.launch(headless=not headed)
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
        # TimeoutError traceback). Poll for the browse URL instead.
        # Hint stays generic on purpose — no chasing site copy changes.
        try:
          page.wait_for_url("**/browse**", timeout=90000)
        except PwTimeout:
          cur = page.url
          if "/login" in cur:
            raise AuthError("browser login timed out staying on the login page "
                            "(check creds / 2FA / CAPTCHA; retry with --headed to watch).")
          raise AuthError(f"browser login timed out at {_debug_redact_url(cur)} — "
                          "site/Cloudflare slow? Retry, or add --headed to watch.")
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
        try:
          browser.close()
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
      fail("watch page rejected — not logged in. Pass --email/--password or --cookies.")
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
       "or a player.vimeo.com config URL.")
