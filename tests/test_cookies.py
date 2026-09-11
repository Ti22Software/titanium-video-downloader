"""Offline tests: per-site cookies (env + explicit file, truth-gated mode)."""

import argparse
import os

import pytest
import requests

from titanium_video_downloader import cli as cli_mod  # noqa: F401 (registers providers)
from titanium_video_downloader.cli import (
  _cookies_label,
  _site_cookies_path,
)
from titanium_video_downloader.core.session import (
  _domain_match,
  load_cookies,
  new_session,
  site_cookie_count,
  site_cookies_env,
)

NETSCAPE = ("# Netscape HTTP Cookie File\n"
            ".rumble.com\tTRUE\t/\tFALSE\t9999999999\tsess\tabc\n"
            ".example.com\tTRUE\t/\tFALSE\t9999999999\tother\txyz\n")

STALE_NETSCAPE = ("# Netscape HTTP Cookie File\n"
                  ".rumble.com\tTRUE\t/\tFALSE\t100\told\tdead\n"
                  ".rumble.com\tTRUE\t/\tFALSE\t9999999999\tfresh\tlive\n")


def _write(path, text):
  path.write_text(text)
  return str(path)


def test_domain_match():
  assert _domain_match(".rumble.com", ("rumble.com",))
  assert _domain_match("rumble.com", ("rumble.com",))
  assert _domain_match("x.rumble.com", ("rumble.com",))
  assert not _domain_match("evilerumble.com", ("rumble.com",))
  assert not _domain_match("rumble.com.evil.com", ("rumble.com",))
  assert not _domain_match("", ("rumble.com",))
  assert not _domain_match(".rumble.com", ())
  assert _domain_match(".watch.studygateway.com", ("studygateway.com",))


def test_site_cookies_env(monkeypatch):
  monkeypatch.setenv("TI22_VIDEO_DL_RUMBLE_COOKIES", "/tmp/r.txt")
  assert site_cookies_env("rumble") == "/tmp/r.txt"
  monkeypatch.delenv("TI22_VIDEO_DL_RUMBLE_COOKIES")
  assert site_cookies_env("rumble") is None
  assert site_cookies_env("") is None


def test_load_cookies_counts_and_warns_on_stale(tmp_path, capsys):
  s = new_session()
  load_cookies(s, _write(tmp_path / "c.txt", NETSCAPE))
  assert site_cookie_count(s, ("rumble.com",)) == 1
  assert site_cookie_count(s, ("vimeo.com",)) == 0
  assert capsys.readouterr().err == ""
  s2 = new_session()
  load_cookies(s2, _write(tmp_path / "s.txt", STALE_NETSCAPE),
               label="TI22_VIDEO_DL_RUMBLE_COOKIES")
  assert site_cookie_count(s2, ("rumble.com",)) == 2
  err = capsys.readouterr().err
  assert "'old' expired" in err and "re-export" in err
  assert "TI22_VIDEO_DL_RUMBLE_COOKIES" in err


def test_load_cookies_missing_file_fails():
  from titanium_video_downloader.extractors.base import AuthError
  with pytest.raises(AuthError):
    load_cookies(new_session(), "/nonexistent/c.txt")


def _args(**kw):
  base = dict(email=None, password=None, cookies=None, prefer_cookies=False,
              no_auth=False, debug_login=False, headed=False, login_only=False,
              list_qualities=False, list_qualities_json=False, pick=False,
              quality="best", on_missing_quality="fallback", output=None,
              output_dir=None, temp_dir=None)
  base.update(kw)
  return argparse.Namespace(**base)


def test_site_cookies_path_matrix(monkeypatch):
  from titanium_video_downloader.extractors.rumble import RumbleAuth
  prov = RumbleAuth()
  assert _site_cookies_path(_args(), prov) == (None, None)
  # explicit path is exclusive (env ignored)
  monkeypatch.setenv("TI22_VIDEO_DL_RUMBLE_COOKIES", "/tmp/env.txt")
  assert _site_cookies_path(_args(cookies="/tmp/exp.txt"), prov) == (
    "explicit", "/tmp/exp.txt")
  # env consulted only when prefer enabled
  assert _site_cookies_path(_args(prefer_cookies=False), prov) == (None, None)
  assert _site_cookies_path(_args(prefer_cookies=True), prov) == (
    "env", "/tmp/env.txt")


def test_cookies_label():
  assert _cookies_label("env", "rumble") == "TI22_VIDEO_DL_RUMBLE_COOKIES"
  assert _cookies_label("explicit", "rumble") == "--cookies"


class _FakeProv:
  site_key = "rumble"
  requires_auth = False
  cookie_domains = ("rumble.com",)

  def resolve_embed(self, session, url, debug=False):
    return "https://rumble.com/embedJS/u3/?v=fake"


def _resolve(monkeypatch, args, session, url="https://rumble.com/vx.html"):
  monkeypatch.setattr(cli_mod, "provider_for", lambda u: _FakeProv())
  return cli_mod._resolve_entry(session, args, url, None, "", False, set(),
                                None, None, None)


def test_explicit_file_truth_gates_mode(monkeypatch, capsys):
  s = new_session()
  s.cookies.set("sess", "abc", domain=".rumble.com", path="/")
  out = _resolve(monkeypatch, _args(cookies="/tmp/r.txt", login_only=True), s)
  assert out["action"] == "loginonly"
  err = capsys.readouterr().err
  assert "using --cookies session (1 cookies for rumble)" in err


def test_explicit_file_foreign_site_falls_back(monkeypatch, capsys):
  s = new_session()  # jar has nothing for rumble
  out = _resolve(monkeypatch, _args(cookies="/tmp/other.txt", login_only=True), s)
  assert out["action"] == "loginonly"
  err = capsys.readouterr().err
  assert "has no cookies for rumble" in err
  assert "needs no login, skipping" in err
  assert "using --cookies" not in err


def test_env_file_first_touch(monkeypatch, tmp_path, capsys):
  _write(tmp_path / "r.txt", NETSCAPE)
  monkeypatch.setenv("TI22_VIDEO_DL_RUMBLE_COOKIES", str(tmp_path / "r.txt"))
  s = new_session()
  args = _args(prefer_cookies=True, login_only=True)
  out = _resolve(monkeypatch, args, s)
  assert out["action"] == "loginonly"
  assert site_cookie_count(s, ("rumble.com",)) == 1
  err = capsys.readouterr().err
  assert "using TI22_VIDEO_DL_RUMBLE_COOKIES session" in err
  # second entry reuses the loaded jar (no reload, still cookies mode)
  capsys.readouterr()
  out = _resolve(monkeypatch, args, s)
  assert out["action"] == "loginonly"
  assert "using TI22_VIDEO_DL_RUMBLE_COOKIES session" in capsys.readouterr().err


def test_prefer_without_env_falls_back(monkeypatch, capsys):
  monkeypatch.delenv("TI22_VIDEO_DL_RUMBLE_COOKIES", raising=False)
  s = new_session()
  out = _resolve(monkeypatch, _args(prefer_cookies=True, login_only=True), s)
  assert out["action"] == "loginonly"
  err = capsys.readouterr().err
  assert "needs no login, skipping" in err


def test_prefer_off_ignores_env(monkeypatch, tmp_path, capsys):
  _write(tmp_path / "r.txt", NETSCAPE)
  monkeypatch.setenv("TI22_VIDEO_DL_RUMBLE_COOKIES", str(tmp_path / "r.txt"))
  s = new_session()
  out = _resolve(monkeypatch, _args(prefer_cookies=False, login_only=True), s)
  assert out["action"] == "loginonly"
  assert site_cookie_count(s, ("rumble.com",)) == 0
  assert "using TI22_VIDEO_DL_RUMBLE_COOKIES" not in capsys.readouterr().err


def test_env_relative_name_resolves_via_config_dir(monkeypatch, tmp_path, capsys):
  work = tmp_path / "work"
  work.mkdir()
  cfg = tmp_path / "cfg"
  cfg.mkdir()
  (cfg / "r.txt").write_text(NETSCAPE)
  monkeypatch.chdir(work)
  monkeypatch.setenv("TI22_VIDEO_DL_CONFIG_DIR", str(cfg))
  monkeypatch.setenv("TI22_VIDEO_DL_RUMBLE_COOKIES", "r.txt")
  s = new_session()
  out = _resolve(monkeypatch, _args(prefer_cookies=True, login_only=True), s)
  assert out["action"] == "loginonly"
  assert site_cookie_count(s, ("rumble.com",)) == 1
  err = capsys.readouterr().err
  assert "config dir" in err
  assert "using TI22_VIDEO_DL_RUMBLE_COOKIES session" in err
