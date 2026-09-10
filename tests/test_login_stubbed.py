"""Offline tests: requests login removed; browser guard rails."""

import sys

import pytest

from titanium_video_downloader.extractors.base import AuthError
from titanium_video_downloader.extractors.studygateway import StudyGatewayAuth


def test_requests_login_removed():
  auth = StudyGatewayAuth()
  with pytest.raises(AuthError, match="not supported"):
    auth.login(object(), "a@b", "pw")


def test_browser_login_needs_playwright(monkeypatch):
  monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
  auth = StudyGatewayAuth()
  with pytest.raises(AuthError, match="playwright not installed"):
    auth.browser_login(object(), "a@b", "pw")


def test_browser_login_needs_creds():
  auth = StudyGatewayAuth()
  with pytest.raises(AuthError, match="needs"):
    auth.browser_login(object(), None, None)
