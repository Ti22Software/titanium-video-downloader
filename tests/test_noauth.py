"""Offline tests: --no-auth dispatch matrix + capability flags."""

import argparse

import pytest

from titanium_video_downloader import cli  # noqa: F401  (registers providers)
from titanium_video_downloader.cli import _auth_mode
from titanium_video_downloader.extractors.studygateway import StudyGatewayAuth
from titanium_video_downloader.extractors.vimeo import VimeoAuth


def _args(**kw):
  base = dict(email=None, password=None, cookies=None, use_browser_login=False,
              no_auth=False, quality="best", list_qualities=False,
              list_qualities_json=False, pick=False)
  base.update(kw)
  return argparse.Namespace(**base)


def test_noauth_vimeo_proceeds():
  assert _auth_mode(_args(no_auth=True), VimeoAuth()) == "none"


def test_noauth_studygateway_fails_fast():
  with pytest.raises(SystemExit):
    _auth_mode(_args(no_auth=True), StudyGatewayAuth())


def test_noauth_conflicts_with_login_opts():
  for kw in (dict(email="a"), dict(password="p"), dict(cookies="c"),
             dict(use_browser_login=True)):
    with pytest.raises(SystemExit):
      _auth_mode(_args(no_auth=True, **kw), VimeoAuth())


def test_auth_mode_matrix():
  sg = StudyGatewayAuth()
  assert _auth_mode(_args(), sg) == "password"
  assert _auth_mode(_args(cookies="c"), sg) == "cookies"
  assert _auth_mode(_args(use_browser_login=True), sg) == "browser"


def test_provider_capability_flags():
  assert StudyGatewayAuth.requires_auth is True
  assert VimeoAuth.requires_auth is False
  assert StudyGatewayAuth.site_key == "studygateway"
  assert VimeoAuth.site_key == "vimeo"
