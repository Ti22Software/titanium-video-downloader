#!/usr/bin/env python3
"""Titanium Downloader (ti22-dl): single-file streaming video downloader.

Pipeline (happy-path login + downstream):
  watch.studygateway.com/.../videos/<slug> --login--> embed.vhx.tv iframe URL
  embed URL (embed.vhx.tv/videos/...) -> window.OTTData.config_url
  config URL (player.vimeo.com/video/.../config?...) -> request.files.dash playlist URL
  playlist.json -> video rendition + AAC audio rendition segment URLs
  segments -> video.mp4 + audio.m4a -> ffmpeg -c copy mux -> out.mp4

Usage:
  ti22-dl EMBED_OR_CONFIG_OR_WATCH_URL [OUTPUT] [--list-qualities] [--quality Q]
                                [--output PATH] [--concurrency N] [--keep-intermediate]
                                [--email E --password P] [--cookies FILE] [--login-only]
"""

import argparse
import base64
import concurrent.futures
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests

try:
  from tqdm import tqdm
except ImportError:
  tqdm = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
EMBED_ORIGIN_HEADERS = {
  "User-Agent": UA,
  "Origin": "https://embed.vhx.tv",
  "Referer": "https://embed.vhx.tv/",
  "Accept": "*/*",
  "Accept-Language": "en-US,en;q=0.5",
}
EMBED_DOC_HEADERS = {
  "User-Agent": UA,
  "Referer": "https://watch.studygateway.com/",
  "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
             "image/avif,image/webp,image/apng,*/*;q=0.8"),
  "Accept-Language": "en-US,en;q=0.5",
  "Upgrade-Insecure-Requests": "1",
}

TIMEOUT = 30
RETRIES = 5

WATCH_BASE = "https://watch.studygateway.com"
WATCH_LOGIN_URL = WATCH_BASE + "/login"
WATCH_BROWSE_URL = WATCH_BASE + "/browse"
WWW_BASE = "https://www.studygateway.com"
WWW_LOGIN_URL = WWW_BASE + "/login"
SAML_POST_URL = WWW_BASE + "/login/saml"

WATCH_DOC_HEADERS = {
  "User-Agent": UA,
  "Referer": "https://watch.studygateway.com/",
  "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
             "image/avif,image/webp,image/apng,*/*;q=0.8"),
  "Accept-Language": "en-US,en;q=0.5",
  "Upgrade-Insecure-Requests": "1",
}
WWW_DOC_HEADERS = {
  "User-Agent": UA,
  "Referer": "https://www.studygateway.com/",
  "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
             "image/avif,image/webp,image/apng,*/*;q=0.8"),
  "Accept-Language": "en-US,en;q=0.5",
  "Upgrade-Insecure-Requests": "1",
}


class AuthError(Exception):
  """Login failed or blocked (recaptcha/cloudflare/2fa/creds)."""


def _host(url):
  """Lowercased hostname, or '' if unparseable. Avoids substring false
  positives (embed URLs carry watch.studygateway.com in query params)."""
  try:
    return (urlparse(url or "").netloc or "").lower()
  except Exception:
    return ""


def _hidden_input(html, name):
  """Return value of <input name=... value=...> or None (no bs4, stdlib only)."""
  m = re.search(
    r'<input[^>]*\bname=["\']' + re.escape(name) + r'["\'][^>]*>',
    html, re.IGNORECASE)
  if not m:
    return None
  tag = m.group(0)
  v = re.search(r'\bvalue=["\']([^"\']*)["\']', tag)
  return v.group(1) if v else ""


def _looks_like_challenge(html):
  """True only for actual bot-block pages, NOT the normal login widget.

  The normal www login page always contains g-recaptcha, so that alone
  must not count as blocked. Only hard challenge markers qualify.
  """
  low = html.lower()
  markers = ("cf-challenge", "challenge-platform", "cf-mitigated",
             "attention required", "access denied",
             "captcha-required", "captcha required",
             "turnstile", "two-factor", "one-time passcode",
             "enter verification code")
  return any(m in low for m in markers)


def _auth_state(html):
  """Authoritative logged-in check. /browse is PUBLIC (200 logged-out), so
  status/url prove nothing. Require user markers seen in authenticated dumps:
  _current_user JSON or logout form. window.TOKEN alone is NOT enough (present
  on generic pages). Returns dict of redacted flags."""
  low = (html or "").lower()
  has_user = "_current_user" in low
  has_logout = "btn-logout-form" in low or "btn-logout" in low
  has_window_token = "window.token" in low
  return {"has_user": has_user, "has_logout": has_logout,
          "has_window_TOKEN": has_window_token,
          "authed": bool(has_user or has_logout)}


def _debug_auth_state(label, html, debug):
  if not debug:
    return {}
  st = _auth_state(html)
  print(f"debug-login: {label} has_user={st['has_user']} "
        f"has_logout={st['has_logout']} has_window_TOKEN={st['has_window_TOKEN']} "
        f"authed={st['authed']}", file=sys.stderr)
  return st


def _debug_redact_url(url):
  """Host + path only, plus bare param names (no token values)."""
  try:
    u = urlparse(url or "")
    return f"{u.scheme}://{u.netloc}{u.path}"
  except Exception:
    return "(unparseable url)"


def _debug_login_state(label, html=None, url=None, status=None, session=None):
  low = (html or "").lower()
  hits = [m for m in ("cf-challenge", "challenge-platform", "cf-mitigated",
                      "attention required", "access denied",
                      "captcha-required", "captcha required",
                      "turnstile", "two-factor", "one-time passcode",
                      "enter verification code") if m in low]
  cookie_names = sorted({c.name for c in session.cookies} if session is not None else [])
  print(f"debug-login: {label} status={status} url={_debug_redact_url(url)} "
        f"len={len(html or '')} challenge_hits={hits} cookies={cookie_names}",
        file=sys.stderr)


def get_creds(args):
  """Precedence: CLI flags > TI22_EMAIL/TI22_PASSWORD env > interactive prompt."""
  email = args.email or os.environ.get("TI22_EMAIL")
  password = args.password or os.environ.get("TI22_PASSWORD")
  if email and password:
    return email, password
  if email and not password and sys.stdin.isatty():
    return email, getpass.getpass("password: ")
  return email, password


def load_cookies(session, path):
  """Load Netscape-format cookies.txt into session (manual fallback)."""
  import http.cookiejar as cj
  jar = cj.MozillaCookieJar(str(path))
  try:
    jar.load(ignore_discard=True, ignore_expires=True)
  except Exception as e:
    raise AuthError(f"could not load --cookies {path}: {e}")
  session.cookies.update(jar)
  return session


class AuthProvider:
  """Pluggable site auth: match(url) / login(session, creds) / resolve_embed()."""

  def match(self, url):
    raise NotImplementedError

  def login(self, session, email, password, debug=False):
    raise NotImplementedError

  def browser_login(self, session, email, password, debug=False, headed=False):
    raise NotImplementedError

  def resolve_embed(self, session, watch_url, debug=False):
    raise NotImplementedError


class StudyGatewayAuth(AuthProvider):
  """Happy-path SAML form login for studygateway (requests-only, no JS)."""

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
    st = _debug_auth_state("browse auth", getattr(b, "text", ""), debug)
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
      from playwright.sync_api import sync_playwright
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
        page.goto(WATCH_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_url("**/login**", timeout=30000)
        www_url = page.url
        if debug:
          print(f"debug-login: browser at {_debug_redact_url(www_url)} "
                f"has_saml={'SAMLRequest=' in www_url}", file=sys.stderr)
        page.fill('input[type="email"], input[name="email"]', email, timeout=15000)
        page.fill('input[type="password"], input[name="password"]', password, timeout=15000)
        with page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
          page.click('button[type="submit"], input[type="submit"]', timeout=15000)
        page.wait_for_url("**/browse**", timeout=60000)
        html = page.content()
        st = _debug_auth_state("browser browse auth", html, debug)
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


PROVIDERS = [StudyGatewayAuth()]


def provider_for(url):
  for p in PROVIDERS:
    if p.match(url):
      return p
  return None


def fail(msg, code=1):
  print(f"error: {msg}", file=sys.stderr)
  sys.exit(code)


def fetch_json(session, url, what):
  try:
    r = session.get(url, headers=EMBED_ORIGIN_HEADERS, timeout=TIMEOUT)
  except requests.RequestException as e:
    fail(f"could not fetch {what}: {e}\n(URLs expire fast — re-paste a fresh URL and retry.)")
  if r.status_code == 410:
    fail(f"{what} token expired.")
  if r.status_code in (401, 403):
    fail(f"{what} rejected (HTTP {r.status_code}) — URL expired. "
         "Re-paste a fresh embed/config URL and retry.")
  if r.status_code != 200:
    fail(f"{what} returned HTTP {r.status_code}.")
  try:
    return r.json()
  except ValueError:
    fail(f"{what} did not return JSON.")


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


def parse_playlist(pl):
  videos = []
  for v in pl.get("video") or []:
    if not (v.get("segments") and v.get("init_segment")):
      continue
    videos.append({
      "id": v.get("id"),
      "width": v.get("width"),
      "height": v.get("height"),
      "bitrate": v.get("bitrate") or 0,
      "codecs": v.get("codecs"),
      "duration": v.get("duration"),
      "framerate": v.get("framerate"),
      "init_segment": v.get("init_segment"),
      "base_url": v.get("base_url") or pl.get("base_url") or "",
      "segments": v.get("segments"),
    })
  audios = []
  for a in pl.get("audio") or []:
    if not (a.get("segments") and a.get("init_segment")):
      continue
    audios.append({
      "id": a.get("id"),
      "bitrate": a.get("bitrate") or 0,
      "codecs": a.get("codecs"),
      "mime_type": a.get("mime_type"),
      "sample_rate": a.get("sample_rate"),
      "channels": a.get("channels"),
      "init_segment": a.get("init_segment"),
      "base_url": a.get("base_url") or pl.get("base_url") or "",
      "segments": a.get("segments"),
    })
  if not videos:
    fail("playlist.json has no video renditions.")
  if not audios:
    fail("playlist.json has no audio renditions.")
  videos.sort(key=lambda v: v["bitrate"], reverse=True)
  return videos, audios


def label_for(v):
  h = v.get("height")
  return f"{h}p" if h else v.get("id", "?")[:8]


def audio_label(codecs):
  c = (codecs or "").lower()
  if c.startswith("mp4a."):
    return "AAC"
  if c.startswith("opus"):
    return "Opus"
  if c.startswith("ec-3") or c.startswith("ac-3"):
    return "Dolby Digital"
  return codecs or "audio"


def select_video(videos, quality):
  if quality in ("best", "highest", "max"):
    return videos[0]
  m = re.fullmatch(r"(\d+)\s*p?", quality.strip().lower())
  if m:
    want = int(m.group(1))
    for v in videos:
      if v.get("height") == want:
        return v
    avail = ", ".join(label_for(v) for v in videos)
    fail(f"quality '{quality}' not available (have: {avail}).")
  # fall back to exact id prefix match
  for v in videos:
    if v.get("id", "").startswith(quality):
      return v
  avail = ", ".join(label_for(v) for v in videos)
  fail(f"unknown quality '{quality}' (have: {avail}, best).")


def select_audio(audios):
  aac = [a for a in audios if (a.get("codecs") or "").startswith("mp4a.")]
  pool = aac or audios
  pool = sorted(pool, key=lambda a: a["bitrate"], reverse=True)
  return pool[0]


def resolve_segments(playlist_url, rendition):
  base = urljoin(playlist_url, rendition["base_url"])
  return [urljoin(base, s["url"]) for s in rendition["segments"]]


WINDOWS_RESERVED_NAMES = {
  "CON", "PRN", "AUX", "NUL",
  *(f"COM{i}" for i in range(1, 10)),
  *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize(name):
  """Portable filename stem: strips the Windows-reserved set (strictest OS),
  which also covers macOS/Linux restrictions. Replacement is always '_'."""
  name = re.sub(r"\s+", " ", name).strip()
  name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")
  if name.upper() in WINDOWS_RESERVED_NAMES:
    name = "_" + name
  return name or "video"


def sanitize_path(p):
  """Sanitize the filename stem of a user-supplied path, keep dir + suffix."""
  p = Path(p)
  return p.parent / (sanitize(p.stem) + p.suffix)


def _get_with_retry(session, url, idx):
  last = None
  for attempt in range(RETRIES):
    try:
      r = session.get(url, headers=EMBED_ORIGIN_HEADERS, timeout=TIMEOUT)
      if r.status_code in (401, 403):
        raise requests.HTTPError(f"HTTP {r.status_code} (URL expired?)")
      r.raise_for_status()
      return idx, r.content
    except Exception as e:
      last = e
      time.sleep(min(2 ** attempt, 8))
  raise RuntimeError(f"segment {idx} failed after {RETRIES} tries: {last}")


def download_rendition(kind, rendition, seg_urls, workdir, session, concurrency):
  parts = workdir / kind
  parts.mkdir(parents=True, exist_ok=True)
  manifest = workdir / f"{kind}.manifest.json"
  done = set()
  if manifest.exists():
    try:
      done = set(json.loads(manifest.read_text()))
    except ValueError:
      done = set()
  todo = [i for i in range(len(seg_urls))
          if i not in done or not (parts / f"{i:05d}.m4s").exists()]
  total = len(seg_urls)
  bar = None
  if tqdm and todo:
    bar = tqdm(total=total, initial=total - len(todo),
               unit="seg", desc=kind)
  elif todo and done:
    print(f"{kind}: {len(done)}/{total} segments cached", file=sys.stderr)
  elif not todo:
    print(f"{kind}: {total}/{total} segments cached", file=sys.stderr)

  errors = []
  lock = threading.Lock()
  start = time.monotonic()
  last_print = [0.0]
  max_width = [0]

  def _write_line(text):
    max_width[0] = max(max_width[0], len(text))
    sys.stderr.write("\r" + text.ljust(max_width[0]))
    sys.stderr.flush()

  def _render(force=False):
    now = time.monotonic()
    if not force and now - last_print[0] < 0.2:
      return
    last_print[0] = now
    n = len(done)
    el = now - start
    rate = n / el if el > 0 and n > 0 else 0.0
    eta = f"{(total - n) / rate:.0f}s" if rate > 0 and n < total else "--"
    _write_line(f"{kind}: {n}/{total} segs, {rate:.1f} seg/s, ETA {eta}")

  def _mark(idx):
    with lock:
      done.add(idx)
      if bar:
        bar.update(1)
      else:
        _render()

  if todo:
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
      futs = {ex.submit(_get_with_retry, session, seg_urls[i], i): i for i in todo}
      try:
        for fut in concurrent.futures.as_completed(futs):
          try:
            idx, data = fut.result()
          except RuntimeError as e:
            errors.append(str(e))
          else:
            (parts / f"{idx:05d}.m4s").write_bytes(data)
            _mark(idx)
      finally:
        if bar:
          bar.close()
      manifest.write_text(json.dumps(sorted(done)))
  if errors:
    fail(f"{kind} download had {len(errors)} failed segment(s), e.g.: {errors[0]}")
  if len(done) != total:
    fail(f"{kind} incomplete: {len(done)}/{total} segments.")
  if not bar:
    el = time.monotonic() - start
    _write_line(f"{kind}: {total}/{total} segs, done in {el:.1f}s")
    sys.stderr.write("\n")
    sys.stderr.flush()

  out = workdir / f"{kind}.mp4"
  with open(out, "wb") as f:
    f.write(base64.b64decode(rendition["init_segment"]))
    for i in range(len(seg_urls)):
      with open(parts / f"{i:05d}.m4s", "rb") as sf:
        shutil.copyfileobj(sf, f)
  return out


def mux(video_path, audio_path, out_path, total_duration=None):
  ff = shutil.which("ffmpeg")
  if not ff:
    fail("ffmpeg not found in PATH.")
  out_path = Path(out_path)
  print(f"\nMuxing -> {out_path.resolve()}", file=sys.stderr)
  cmd = [ff, "-y", "-v", "error", "-nostats",
         "-i", str(video_path), "-i", str(audio_path),
         "-c", "copy", "-movflags", "+faststart",
         "-progress", "pipe:1", str(out_path)]
  total_us = int(total_duration * 1_000_000) if total_duration else None
  start = time.monotonic()
  last_print = [0.0]
  max_width = [0]

  def _write_mux_line(text):
    max_width[0] = max(max_width[0], len(text))
    sys.stderr.write("\r" + text.ljust(max_width[0]))
    sys.stderr.flush()

  proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)
  try:
    for line in proc.stdout:
      line = line.strip()
      if not line.startswith("out_time_ms="):
        continue
      try:
        cur = int(line.split("=", 1)[1])
      except ValueError:
        continue
      if cur <= 0:
        continue
      now = time.monotonic()
      if now - last_print[0] < 0.2:
        continue
      last_print[0] = now
      el = now - start
      if total_us:
        frac = min(cur / total_us, 1.0)
        eta = el * (1 - frac) / frac if frac > 0 else 0.0
        _write_mux_line(f"Muxing: {frac * 100:.0f}%, ETA {eta:.0f}s")
      else:
        _write_mux_line(f"Muxing: {el:.0f}s elapsed...")
  finally:
    if proc.stdout:
      proc.stdout.close()
  rc = proc.wait()
  err = proc.stderr.read() if proc.stderr else ""
  if proc.stderr:
    proc.stderr.close()
  if rc != 0:
    print(err[-3000:], file=sys.stderr)
    fail("ffmpeg mux failed.")
  el = time.monotonic() - start
  _write_mux_line(f"Muxing: done in {el:.1f}s")
  sys.stderr.write("\n")
  sys.stderr.flush()
  return out_path


def fetch_text(session, url):
  try:
    r = session.get(url, headers=EMBED_DOC_HEADERS, timeout=TIMEOUT)
  except requests.RequestException as e:
    fail(f"could not fetch embed page: {e}")
  if r.status_code in (401, 403):
    fail("embed page rejected — session/token expired. Re-run to mint a fresh "
         "embed URL via login (tokens are hours-lived, not for pasting).")
  if r.status_code != 200:
    fail(f"embed page returned HTTP {r.status_code}.")
  return r.text


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


def main(argv=None):
  ap = argparse.ArgumentParser(
    description="Download a single studygateway video to an MP4 file.")
  ap.add_argument("input_url", help="watch.studygateway.com video URL, embed.vhx.tv iframe URL, or player.vimeo.com config URL")
  ap.add_argument("output_pos", nargs="?", default=None,
                  help="output MP4 path (shorthand for --output)")
  ap.add_argument("--list-qualities", action="store_true",
                  help="list available renditions and exit")
  ap.add_argument("--quality", default="best",
                  help="height label (1080p/720p/540p/360p/240p), 'best', or rendition id prefix")
  ap.add_argument("--output", "-o", default=None, help="output MP4 path")
  ap.add_argument("--concurrency", "-j", type=int, default=4,
                  help="parallel segment downloads (default 4)")
  ap.add_argument("--keep-intermediate", action="store_true",
                  help="keep video/audio intermediates and parts dir")
  ap.add_argument("--email", default=None, help="login email (or TI22_EMAIL)")
  ap.add_argument("--password", default=None, help="login password (or TI22_PASSWORD)")
  ap.add_argument("--cookies", default=None, help="Netscape cookies.txt fallback for bot-blocked login (export logged-in browser cookies for .studygateway.com)")
  ap.add_argument("--login-only", action="store_true",
                  help="login + resolve embed URL, print it, and exit")
  ap.add_argument("--debug-login", action="store_true",
                  help="print redacted login diagnostics (urls, parse flags, cookies, challenge hits)")
  ap.add_argument("--use-browser-login", action="store_true",
                  help="real Chromium login via Playwright (passes reCAPTCHA v3); exports cookies to pipeline")
  ap.add_argument("--headed", action="store_true",
                  help="show browser window with --use-browser-login (debug 2FA/CAPTCHA)")
  args = ap.parse_args(argv)
  if args.output and args.output_pos and args.output != args.output_pos:
    fail("pass the output path either positionally or via --output, not both.")
  output_arg = args.output or args.output_pos

  session = requests.Session()
  session.headers.update({"User-Agent": UA})

  if args.cookies:
    try:
      load_cookies(session, args.cookies)
    except AuthError as e:
      fail(str(e))

  input_url = args.input_url
  prov = provider_for(input_url)
  if prov is not None:
    if args.cookies and not args.use_browser_login:
      print("note: using --cookies session, skipping password login", file=sys.stderr)
    else:
      email, password = get_creds(args)
      try:
        if args.use_browser_login:
          prov.browser_login(session, email, password,
                             debug=args.debug_login, headed=args.headed)
        else:
          prov.login(session, email, password, debug=args.debug_login)
      except AuthError as e:
        fail(f"{e}")
      print("login ok", file=sys.stderr)
    try:
      input_url = prov.resolve_embed(session, args.input_url, debug=args.debug_login)
    except SystemExit:
      raise
    except Exception as e:
      fail(f"could not resolve embed URL: {e}")
    print(f"embed: {input_url}", file=sys.stderr)
    if args.login_only:
      print(input_url)
      return 0

  config_url, title, _vid = resolve_config_url(session, input_url)
  config = fetch_json(session, config_url, "player config")
  playlist_url, fallback_url, qmap = pick_playlist_url(config)

  playlist = None
  for url in [u for u in (playlist_url, fallback_url) if u]:
    try:
      r = session.get(url, headers=EMBED_ORIGIN_HEADERS, timeout=TIMEOUT)
      if r.status_code in (401, 403):
        continue
      r.raise_for_status()
      playlist = r.json()
      playlist_url = url
      break
    except (requests.RequestException, ValueError):
      continue
  if playlist is None:
    fail("playlist.json fetch failed on all CDNs — URLs expired. "
         "Re-paste a fresh URL (embed page first, config within 60s).")

  videos, audios = parse_playlist(playlist)

  if args.list_qualities:
    print(f"{'quality':<8}{'size':>10}  {'bitrate':>9}  id")
    for v in sorted(videos, key=lambda v: v.get("height") or 0, reverse=True):
      total = sum(s.get("size", 0) for s in v["segments"])
      q = qmap.get(v["id"], label_for(v))
      print(f"{q:<8}{total / 1e6:>9.1f}M  {v['bitrate']:>9}  {v['id']}")
    return 0

  video = select_video(videos, args.quality)
  audio = select_audio(audios)
  v_urls = resolve_segments(playlist_url, video)
  a_urls = resolve_segments(playlist_url, audio)

  name = sanitize(title or config.get("video", {}).get("title") or "video")
  q = qmap.get(video["id"], label_for(video))
  if output_arg:
    out_path = sanitize_path(output_arg)
    if Path(output_arg).name != out_path.name:
      print(f"note: sanitized output name to '{out_path.name}'", file=sys.stderr)
  else:
    out_path = Path(f"{name} [{q}].mp4")
  if out_path.exists():
    fail(f"output exists: {out_path} (remove it or pass a different output path).")
  if out_path.suffix.lower() != ".mp4":
    out_path = out_path.with_suffix(".mp4")

  workdir = out_path.parent / (out_path.stem + ".ti22")
  workdir.mkdir(parents=True, exist_ok=True)
  fps = f" {video['framerate']:.2f}fps" if video.get("framerate") else ""
  sr = audio.get("sample_rate")
  sr_label = f" {sr / 1000:.1f}kHz" if sr else ""
  print(f"video: {q} {video['width']}x{video['height']}{fps} "
        f"({len(v_urls)} segs), "
        f"audio: {audio_label(audio.get('codecs'))}{sr_label} ({len(a_urls)} segs)",
        file=sys.stderr)
  v_path = download_rendition("video", video, v_urls, workdir, session, args.concurrency)
  a_path = download_rendition("audio", audio, a_urls, workdir, session, args.concurrency)
  mux(v_path, a_path, out_path, total_duration=video.get("duration"))
  if not args.keep_intermediate:
    shutil.rmtree(workdir, ignore_errors=True)
  print(str(out_path))
  return 0


if __name__ == "__main__":
  sys.exit(main())
