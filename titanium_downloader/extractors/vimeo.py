"""Public Vimeo extractor (planned Phase 2a — stub).

Plan: reuse the config→playlist path; no-auth resolve from the public
player page. Not implemented yet.
"""

from .base import AuthError, AuthProvider, _host


class VimeoAuth(AuthProvider):
  """Public Vimeo: match real, login is a no-op, resolve waits for Phase 2a."""

  site_key = "vimeo"
  requires_auth = False

  def match(self, url):
    # Watch pages only: player.vimeo.com/config URLs are a downstream
    # artifact handled by resolve_config_url, and must bypass providers.
    return _host(url) in ("vimeo.com", "www.vimeo.com")

  def login(self, session, email, password, debug=False):
    return session

  def browser_login(self, session, email, password, debug=False, headed=False):
    return session

  def resolve_embed(self, session, watch_url, debug=False):
    raise AuthError("vimeo resolve not implemented (planned Phase 2a).")
