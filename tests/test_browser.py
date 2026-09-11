"""Offline tests: shared-browser singleton (no real Chromium)."""

import sys
import types

import pytest

from titanium_video_downloader.core import browser as browser_mod
from titanium_video_downloader.core.browser import close_browser, get_browser
from titanium_video_downloader.extractors.base import AuthError


class _Dead(Exception):
  pass


def _fake_pw_module(launches):
  mod = types.ModuleType("playwright.sync_api")
  mod.TimeoutError = _Dead

  class _Browser:
    def __init__(self):
      self.closed = False

    def is_connected(self):
      return not self.closed

    def close(self):
      self.closed = True

  class _Chromium:
    @staticmethod
    def launch(headless=True):
      launches.append(headless)
      return _Browser()

  class _Pw:
    chromium = _Chromium()

    def stop(self):
      pass

  class _Sync:
    def start(self):
      return _Pw()

  mod.sync_playwright = lambda: _Sync()
  return mod


@pytest.fixture(autouse=True)
def _fresh_browser():
  close_browser()
  yield
  close_browser()


def test_launch_once_across_calls(monkeypatch):
  launches = []
  monkeypatch.setitem(sys.modules, "playwright.sync_api", _fake_pw_module(launches))
  first = get_browser()
  second = get_browser()
  assert first is second
  assert launches == [True]


def test_headed_mismatch_relaunches(monkeypatch):
  launches = []
  monkeypatch.setitem(sys.modules, "playwright.sync_api", _fake_pw_module(launches))
  get_browser(headed=False)
  get_browser(headed=True)
  assert launches == [True, False]


def test_close_resets(monkeypatch):
  launches = []
  monkeypatch.setitem(sys.modules, "playwright.sync_api", _fake_pw_module(launches))
  get_browser()
  close_browser()
  get_browser()
  assert launches == [True, True]


def test_dead_browser_relaunches(monkeypatch):
  launches = []
  monkeypatch.setitem(sys.modules, "playwright.sync_api", _fake_pw_module(launches))
  browser = get_browser()
  browser.close()
  assert get_browser() is not browser
  assert launches == [True, True]


def test_missing_playwright_clean_error(monkeypatch):
  monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
  with pytest.raises(AuthError, match="playwright not installed"):
    get_browser()
