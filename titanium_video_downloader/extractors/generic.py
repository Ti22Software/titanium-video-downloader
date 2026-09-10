"""Generic HLS/DASH-from-page-HTML fallback slot for the unknown site.

Never auto-matches (match returns False); a future unknown-site module
takes precedence via the provider registry. Kept so the ABC has a home
for site-agnostic fallback logic (Phase 3 unknown-site work).
"""

from .base import AuthError, AuthProvider


class GenericAuth(AuthProvider):
  """Fallback slot: never matches yet, everything else unimplemented."""

  site_key = "generic"
  requires_auth = True

  def match(self, url):
    return False

  def login(self, session, email, password, debug=False):
    raise AuthError("generic login not implemented (unknown-site slot).")

  def resolve_embed(self, session, watch_url, debug=False):
    raise AuthError("generic resolve not implemented (unknown-site slot).")
