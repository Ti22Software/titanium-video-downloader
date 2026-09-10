"""Offline tests: .env parsing, lookup order, setdefault, per-site creds."""

import argparse
from pathlib import Path

from titanium_video_downloader.core import envfile
from titanium_video_downloader.core.envfile import (
  default_config_dir,
  find_dotenv,
  load_dotenv,
  parse_dotenv_text,
)
from titanium_video_downloader.core.session import get_creds


def test_parse_basic_and_quotes():
  pairs, bad = parse_dotenv_text(
    "# comment\n"
    "\n"
    "TI22_STUDYGATEWAY_EMAIL=a@b\n"
    'TI22_STUDYGATEWAY_PASSWORD="p@ss w"\n'
    "TI22_VIMEO_EMAIL='v@v'\n"
  )
  assert pairs == {"TI22_STUDYGATEWAY_EMAIL": "a@b",
                   "TI22_STUDYGATEWAY_PASSWORD": "p@ss w",
                   "TI22_VIMEO_EMAIL": "v@v"}
  assert bad == []


def test_parse_bad_lines_reported():
  pairs, bad = parse_dotenv_text("NOEQUALS\n123BAD=x\nok=y\n")
  assert pairs == {"ok": "y"}
  assert bad == [1, 2]


def test_load_respects_real_env(monkeypatch, tmp_path):
  f = tmp_path / ".env"
  f.write_text("TI22_TEST_X=fromfile\nTI22_TEST_Y=fromfile\n")
  monkeypatch.setenv("TI22_TEST_Y", "realenv")
  assert load_dotenv(f) == f
  import os
  assert os.environ["TI22_TEST_X"] == "fromfile"
  assert os.environ["TI22_TEST_Y"] == "realenv"


def test_load_missing_returns_none(tmp_path):
  assert load_dotenv(tmp_path / "nope.env") is None


def test_default_config_dir_override(monkeypatch, tmp_path):
  monkeypatch.setenv("TI22_CONFIG_DIR", str(tmp_path))
  assert default_config_dir() == tmp_path


def test_default_config_dir_vendor_layout(monkeypatch):
  import os
  monkeypatch.delenv("TI22_CONFIG_DIR", raising=False)
  d = default_config_dir()
  if os.name == "nt":
    assert d.parts[-2:] == ("titanium-software", "ti22-video-dl")
  else:
    assert d == Path.home() / ".config" / "titanium-software" / "ti22-video-dl"


def test_find_dotenv_prefers_cwd(monkeypatch, tmp_path):
  cwd_env = tmp_path / ".env"
  cwd_env.write_text("A=1\n")
  monkeypatch.chdir(tmp_path)
  monkeypatch.delenv("TI22_CONFIG_DIR", raising=False)
  assert find_dotenv() == cwd_env


def _args(**kw):
  base = dict(email=None, password=None)
  base.update(kw)
  return argparse.Namespace(**base)


def test_get_creds_precedence(monkeypatch):
  for var in ("TI22_STUDYGATEWAY_EMAIL", "TI22_STUDYGATEWAY_PASSWORD",
              "TI22_EMAIL", "TI22_PASSWORD"):
    monkeypatch.delenv(var, raising=False)
  e, p, src = get_creds(_args(), "studygateway")
  assert (e, p, src) == (None, None, "missing")
  monkeypatch.setenv("TI22_EMAIL", "gen@x")
  monkeypatch.setenv("TI22_PASSWORD", "genpw")
  e, p, src = get_creds(_args(), "studygateway")
  assert (e, p, src) == ("gen@x", "genpw", "env")
  monkeypatch.setenv("TI22_STUDYGATEWAY_EMAIL", "site@x")
  e, p, src = get_creds(_args(), "studygateway")
  assert (e, p, src) == ("site@x", "genpw", "site-env")
  e, p, src = get_creds(_args(email="flag@x"), "studygateway")
  assert (e, p, src) == ("flag@x", "genpw", "flag")
