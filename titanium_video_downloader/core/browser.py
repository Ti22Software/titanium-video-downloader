"""Process-shared headless browser (batch amortization).

Playwright's sync API is single-threaded: resolve/login phases stay
sequential and share one browser process across videos; per-call contexts
preserve cookie isolation. Single-download behavior is unchanged
(launch → use → exit-time close instead of immediate close).

Threading note: do not share the browser across threads. Segment
downloading (already threaded) never touches it — browser use is confined
to resolve/login, which stay sequential under batch too.
"""

_pw = None
_browser = None
_headed = False


def get_browser(headed=False):
  """Return the shared browser, launching (or relaunching) as needed."""
  global _pw, _browser, _headed
  if _browser is not None and headed == _headed:
    try:
      if _browser.is_connected():
        return _browser
    except Exception:
      pass
    close_browser()
  else:
    close_browser()
  try:
    from playwright.sync_api import sync_playwright
  except ImportError:
    from ..extractors.base import AuthError
    raise AuthError("playwright not installed — run: pip install playwright "
                    "&& playwright install chromium, or use --cookies.")
  _pw = sync_playwright().start()
  _browser = _pw.chromium.launch(headless=not headed)
  _headed = headed
  return _browser


def close_browser():
  """Best-effort shutdown (safe to call with nothing open)."""
  global _pw, _browser, _headed
  for obj, meth in ((_browser, "close"), (_pw, "stop")):
    try:
      if obj is not None:
        getattr(obj, meth)()
    except Exception:
      pass
  _pw, _browser, _headed = None, None, False
