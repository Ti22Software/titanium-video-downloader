"""Offline tests: stubbed SAML login (public browse rejects, authed passes)."""

import pytest

from titanium_downloader.extractors.base import AuthError
from titanium_downloader.extractors.studygateway import StudyGatewayAuth


class _Resp:
  def __init__(self, status=200, url="", text="", headers=None):
    self.status_code = status
    self.url = url
    self.text = text
    self.headers = headers or {}


def _session(login_html, browse_html):
  class _Stub:
    def get(self, url, headers=None, timeout=None, allow_redirects=True):
      if "watch.studygateway.com/login" in url and not allow_redirects:
        return _Resp(302, url, "",
                     {"Location": "https://www.studygateway.com/login?SAMLRequest=SREQ"})
      if "watch.studygateway.com/login" in url:
        return _Resp(200, url, "seed")
      if "www.studygateway.com/login" in url:
        return _Resp(200, "https://www.studygateway.com/login?SAMLRequest=SREQ",
                     login_html)
      if "watch.studygateway.com/browse" in url:
        return _Resp(200, "https://watch.studygateway.com/browse", browse_html)
      return _Resp(200, url, "")

    def post(self, url, data=None, headers=None, timeout=None, allow_redirects=True):
      return _Resp(200, url, "<html>login result</html>")
  return _Stub()


LOGIN_HTML = ('<input name="_token" value="TOKEN1234567890">'
              '<input name="SAMLRequest" value="SAMLREQ">')
PUBLIC_BROWSE = "<html><body>public listing</body></html>"
AUTHED_BROWSE = ('<html><script>window.dataLayer.push({"_current_user": '
                 '{"id": 81029639}})</script>'
                 '<form class="btn-logout-form"></form></html>')


def test_login_rejects_public_browse():
  auth = StudyGatewayAuth()
  with pytest.raises(AuthError, match="rejected"):
    auth.login(_session(LOGIN_HTML, PUBLIC_BROWSE), "a@b", "pw")


def test_login_accepts_authed_browse():
  auth = StudyGatewayAuth()
  session = _session(LOGIN_HTML, AUTHED_BROWSE)
  assert auth.login(session, "a@b", "pw") is session


def test_login_needs_creds():
  auth = StudyGatewayAuth()
  with pytest.raises(AuthError, match="needs"):
    auth.login(_session(LOGIN_HTML, AUTHED_BROWSE), None, None)
