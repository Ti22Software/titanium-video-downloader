"""Login-capable session: constants, credentials, cookie jar."""

import getpass
import os
import sys

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
