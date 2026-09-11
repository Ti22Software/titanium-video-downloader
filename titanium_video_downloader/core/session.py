"""Login-capable session: constants, credentials, cookie jar."""

import getpass
import os
import sys
import time

import requests

from ..extractors.base import AuthError

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


def new_session():
  session = requests.Session()
  session.headers.update({"User-Agent": UA})
  return session


def _cred_source(args, site_key):
  """Redacted credential origin for --debug-login (names only, never values)."""
  if args.email or args.password:
    return "flag"
  key = (site_key or "").upper()
  if key and (os.environ.get(f"TI22_VIDEO_DL_{key}_EMAIL")
              or os.environ.get(f"TI22_VIDEO_DL_{key}_PASSWORD")):
    return "site-env"
  return "missing"


def get_creds(args, site_key=None):
  """Return (email, password, source) for a site.

  Per-field precedence: CLI flags > TI22_VIDEO_DL_<SITE>_EMAIL/PASSWORD,
  then interactive password prompt when an email is known and stdin is a
  tty. There is deliberately no generic fallback: app-qualified names keep
  shared shell scopes collision-free across Titanium packages. Source is
  redacted metadata for --debug-login; values are never logged.
  """
  key = (site_key or "").upper()
  email = args.email
  password = args.password
  if email is None and key:
    email = os.environ.get(f"TI22_VIDEO_DL_{key}_EMAIL")
  if password is None and key:
    password = os.environ.get(f"TI22_VIDEO_DL_{key}_PASSWORD")
  source = _cred_source(args, site_key)
  if email and password:
    return email, password, source
  if email and sys.stdin.isatty():
    return email, getpass.getpass("password: "), "prompt"
  return email, password, source


def load_cookies(session, path, label="--cookies"):
  """Load Netscape-format cookies.txt into session (manual fallback).

  Expired entries still load (servers decide, not us) but each earns a
  stderr note naming the cookie — never values — so a stale file fails
  loudly at resolve with a re-export pointer instead of silent confusion.
  """
  import http.cookiejar as cj
  jar = cj.MozillaCookieJar(str(path))
  try:
    jar.load(ignore_discard=True, ignore_expires=True)
  except Exception as e:
    raise AuthError(f"could not load {label} {path}: {e}")
  now = time.time()
  for c in jar:
    try:
      expired = bool(c.expires) and c.expires < now
    except Exception:
      expired = False
    if expired:
      print(f"note: {label} cookie '{c.name}' expired ({path}) — "
            f"re-export if login fails", file=sys.stderr)
  session.cookies.update(jar)
  return session


def site_cookies_env(site_key):
  """Per-site cookies-file path from TI22_VIDEO_DL_<SITE>_COOKIES, or None."""
  key = (site_key or "").upper()
  if not key:
    return None
  path = os.environ.get(f"TI22_VIDEO_DL_{key}_COOKIES")
  return path or None


def _domain_match(cookie_domain, domains):
  """True when a jar cookie domain belongs to one of the site domains.

  Leading-dot (domain cookie) and bare (host-only) forms both normalize;
  matching is exact-or-suffix on dot boundaries so `evilexample.com`
  never matches `example.com`.
  """
  cand = (cookie_domain or "").lower().lstrip(".")
  for dom in domains or ():
    dom = (dom or "").lower().lstrip(".")
    if not dom:
      continue
    if cand == dom or cand.endswith("." + dom):
      return True
  return False


def site_cookie_count(session, domains):
  """Number of session-jar cookies belonging to the site domains."""
  try:
    jar = session.cookies
  except AttributeError:
    return 0
  try:
    cookies = list(jar)
  except TypeError:
    return 0
  return sum(1 for c in cookies
             if _domain_match(getattr(c, "domain", ""), domains))
