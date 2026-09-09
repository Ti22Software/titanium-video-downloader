"""RightNow Media extractor (stub only).

Login-required site. UNVERIFIED hypothesis: a similar OTT shape to
StudyGateway — to be confirmed by a real probe (login flow calls + DRM
check) before any implementation. No shared-code assumptions baked in;
dedup happens after the probe, not now.
"""

from .base import AuthError, AuthProvider, _host


class RightNowMediaAuth(AuthProvider):
  """RightNow Media: match real, everything else waits for the probe."""

  site_key = "rightnowmedia"
  requires_auth = True

  def match(self, url):
    return "rightnowmedia" in _host(url)

  def login(self, session, email, password, debug=False):
    raise AuthError("rightnowmedia login not implemented (stub — probe pending).")

  def resolve_embed(self, session, watch_url, debug=False):
    raise AuthError("rightnowmedia resolve not implemented (stub — probe pending).")
