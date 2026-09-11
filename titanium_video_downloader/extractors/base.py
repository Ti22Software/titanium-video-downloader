"""Shared auth plumbing: errors, host/helpers, provider registry.

Dependency rule: this module imports nothing from core.* or sibling
extractors, so the DAG stays acyclic::

  base <- core.session, core.models, core.fetcher <- extractors.* <- cli

core.* imports ``fail``/``AuthError`` from here.
"""

import re
import sys
from urllib.parse import urlparse


class AuthError(Exception):
  """Login failed or blocked (recaptcha/cloudflare/2fa/creds)."""


def fail(msg, code=1):
  print(f"error: {msg}", file=sys.stderr)
  sys.exit(code)


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


class AuthProvider:
  """Pluggable site auth: match(url) / login(session, creds) / resolve_embed().

  site_key drives per-site env names (TI22_VIDEO_DL_<SITE>_EMAIL) and debug labels.
  requires_auth gates --no-auth: sites that mint tokens server-side must
  refuse tokenless runs fail-fast instead of 401ing downstream.
  cookie_domains lists the cookie domains belonging to the site, for the
  per-site cookies truth-check (does this jar actually serve this site?).
  """

  site_key = "generic"
  requires_auth = True
  cookie_domains = ()

  def match(self, url):
    raise NotImplementedError

  def login(self, session, email, password, debug=False):
    raise NotImplementedError

  def browser_login(self, session, email, password, debug=False, headed=False):
    raise AuthError(f"{type(self).__name__} browser login not implemented.")

  def resolve_embed(self, session, watch_url, debug=False):
    raise NotImplementedError


PROVIDERS = []


def register_provider(provider):
  """Register a site provider (called explicitly by cli, no import magic)."""
  PROVIDERS.append(provider)
  return provider


def provider_for(url):
  for p in PROVIDERS:
    if p.match(url):
      return p
  return None
