"""Offline tests: --no-auth dispatch matrix + capability flags.

Login is automatic, never a user decision: auth-required sites always
harvest via browser, no-auth sites skip. --no-auth only skips (no-auth
sites) or fails fast (auth sites); it conflicts with layered login
options. Explicit CLI login signals override a layered config no_auth
via _apply_noauth_override. Cookie preference (--prefer-cookies /
prefer_cookies) gates per-site env lookup; CLI --no-auth + CLI
--prefer-cookies is a hard conflict, layered contradictions resolve by
CLI-beats-config or safest-direction with a note.
"""

import argparse

import pytest

from titanium_video_downloader import cli  # noqa: F401  (registers providers)
from titanium_video_downloader.cli import _apply_noauth_override, _auth_mode
from titanium_video_downloader.extractors.studygateway import StudyGatewayAuth
from titanium_video_downloader.extractors.vimeo import VimeoAuth


def _args(**kw):
  base = dict(email=None, password=None, cookies=None, prefer_cookies=False,
              headed=False, no_auth=False, quality="best", list_qualities=False,
              list_qualities_json=False, pick=False)
  base.update(kw)
  return argparse.Namespace(**base)


def test_noauth_vimeo_proceeds():
  assert _auth_mode(_args(no_auth=True), VimeoAuth()) == "none"


def test_noauth_studygateway_fails_fast():
  with pytest.raises(SystemExit):
    _auth_mode(_args(no_auth=True), StudyGatewayAuth())


def test_noauth_conflicts_with_login_opts():
  for kw in (dict(email="a"), dict(password="p"), dict(cookies="c")):
    with pytest.raises(SystemExit):
      _auth_mode(_args(no_auth=True, **kw), VimeoAuth())


def test_auth_mode_matrix():
  sg = StudyGatewayAuth()
  # auth-required sites always harvest via browser; site cookies win
  assert _auth_mode(_args(), sg) == "browser"
  assert _auth_mode(_args(), sg, cookies_for_site=True) == "cookies"
  # no-auth sites skip by default (password mode, no creds → skip note)
  assert _auth_mode(_args(), VimeoAuth()) == "password"
  assert _auth_mode(_args(), VimeoAuth(), cookies_for_site=True) == "cookies"


def test_noauth_override_explicit_cli_wins():
  # CLI --no-auth itself always wins (it is explicit).
  ns = _args(no_auth=True)
  assert _apply_noauth_override(ns, {"no_auth"}) is True
  # Layered config no_auth + explicit CLI creds → log in with a note.
  ns = _args(no_auth=True, email="a@b.c")
  assert _apply_noauth_override(ns, set()) is False
  # Layered config no_auth + explicit CLI --prefer-cookies → prefer wins.
  ns = _args(no_auth=True, prefer_cookies=True)
  assert _apply_noauth_override(ns, {"prefer_cookies"}) is False
  # Both layered → no_auth wins (safest direction).
  ns = _args(no_auth=True, prefer_cookies=True)
  assert _apply_noauth_override(ns, set()) is True
  # Layered config no_auth, no explicit signals → skip.
  ns = _args(no_auth=True)
  assert _apply_noauth_override(ns, set()) is True
  # Nothing layered → normal.
  ns = _args()
  assert _apply_noauth_override(ns, set()) is False


def test_noauth_override_explicit_headed_wins(capsys):
  # CLI --headed is login intent: beats layered config no_auth.
  ns = _args(no_auth=True, headed=True)
  assert _apply_noauth_override(ns, {"headed"}) is False
  assert "explicit --headed overrides config no_auth" in capsys.readouterr().err
  # Layered config headed=true is NOT intent: no_auth stands.
  ns = _args(no_auth=True, headed=True)
  assert _apply_noauth_override(ns, set()) is True


def test_noauth_fail_names_config_source(capsys):
  with pytest.raises(SystemExit):
    _auth_mode(_args(no_auth=True), StudyGatewayAuth())
  assert "config no_auth=false" in capsys.readouterr().err


def test_cli_noauth_prefer_conflict():
  from titanium_video_downloader.cli import main
  with pytest.raises(SystemExit) as ei:
    main(["--no-auth", "--prefer-cookies", "https://vimeo.com/1"])
  assert ei.value.code == 1


def test_cli_noauth_headed_conflict():
  from titanium_video_downloader.cli import main
  with pytest.raises(SystemExit) as ei:
    main(["--no-auth", "--headed", "https://vimeo.com/1"])
  assert ei.value.code == 1


def test_provider_capability_flags():
  assert StudyGatewayAuth.requires_auth is True
  assert VimeoAuth.requires_auth is False
  assert StudyGatewayAuth.site_key == "studygateway"
  assert VimeoAuth.site_key == "vimeo"
