"""Offline tests: host rules, challenge tightness, auth markers, registry."""

from titanium_downloader import cli  # noqa: F401  (registers providers)
from titanium_downloader.extractors.base import (
  _auth_state,
  _hidden_input,
  _host,
  _looks_like_challenge,
  provider_for,
)
from titanium_downloader.extractors.studygateway import StudyGatewayAuth
from titanium_downloader.extractors.vimeo import VimeoAuth


def test_host_avoids_query_false_positive():
  embed = ("https://embed.vhx.tv/videos/3553719?api=1"
           "&referrer=https%3A%2F%2Fwatch.studygateway.com%2F")
  assert _host("https://watch.studygateway.com/slug/videos/x") == "watch.studygateway.com"
  assert _host(embed) == "embed.vhx.tv"
  assert _host("not a url %%") == ""


def test_hidden_input_parse():
  html = '<input name="_token" value="abc123"><input name="SAMLRequest" value="REQ">'
  assert _hidden_input(html, "_token") == "abc123"
  assert _hidden_input(html, "SAMLRequest") == "REQ"
  assert _hidden_input(html, "missing") is None


def test_challenge_ignores_normal_widget():
  assert not _looks_like_challenge('<script src="recaptcha/api.js"></script>'
                                   '<div class="g-recaptcha"></div>')
  assert not _looks_like_challenge("__cf_bm cookie and sso login page")


def test_challenge_detects_real_blocks():
  assert _looks_like_challenge("cf-challenge challenge-platform")
  assert _looks_like_challenge("Attention Required — Cloudflare")
  assert _looks_like_challenge("captcha required to continue")


def test_auth_state_requires_user_markers():
  authed = ('<script>window.dataLayer.push({"_current_user": {"id": 1}})</script>'
            '<form class="btn-logout-form"></form><script>window.TOKEN="x"</script>')
  st = _auth_state(authed)
  assert st["authed"] and st["has_user"] and st["has_logout"]


def test_auth_state_public_browse_not_authed():
  public = "<html><body>browse listing, window.TOKEN=\"app-public\"</body></html>"
  st = _auth_state(public)
  assert not st["authed"]
  assert st["has_window_TOKEN"]  # present but insufficient on its own


def test_provider_registry_routing():
  assert isinstance(provider_for("https://watch.studygateway.com/a/b/videos/c"),
                      StudyGatewayAuth)
  assert provider_for("https://embed.vhx.tv/videos/1?referrer="
                      "https://watch.studygateway.com%2F") is None
  # vimeo stub claims watch pages (resolve waits for Phase 2a);
  # raw config URLs still bypass providers to resolve_config_url.
  assert isinstance(provider_for("https://vimeo.com/123456789"), VimeoAuth)
  assert provider_for("https://player.vimeo.com/video/1/config?token=x") is None
