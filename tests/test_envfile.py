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
  monkeypatch.setenv("TI22_VIDEO_DL_CONFIG_DIR", str(tmp_path))
  assert default_config_dir() == tmp_path


def test_default_config_dir_vendor_layout(monkeypatch):
  import os
  monkeypatch.delenv("TI22_VIDEO_DL_CONFIG_DIR", raising=False)
  d = default_config_dir()
  if os.name == "nt":
    assert d.parts[-2:] == ("titanium-software", "ti22-video-dl")
  else:
    assert d == Path.home() / ".config" / "titanium-software" / "ti22-video-dl"


def test_find_dotenv_prefers_cwd(monkeypatch, tmp_path):
  cwd_env = tmp_path / ".env"
  cwd_env.write_text("A=1\n")
  monkeypatch.chdir(tmp_path)
  monkeypatch.delenv("TI22_VIDEO_DL_CONFIG_DIR", raising=False)
  assert find_dotenv() == cwd_env


def test_tracked_example_parses_cleanly():
  from pathlib import Path
  example = Path(__file__).resolve().parent.parent / ".env.example"
  pairs, bad = parse_dotenv_text(example.read_text(encoding="utf-8"))
  assert bad == []
  for site in ("STUDYGATEWAY", "VIMEO", "RUMBLE", "RIGHTNOWMEDIA", "GENERIC"):
    assert f"TI22_VIDEO_DL_{site}_EMAIL" in pairs
    assert f"TI22_VIDEO_DL_{site}_PASSWORD" in pairs


def _args(**kw):
  base = dict(email=None, password=None)
  base.update(kw)
  return argparse.Namespace(**base)


def test_get_creds_precedence(monkeypatch):
  for var in ("TI22_VIDEO_DL_STUDYGATEWAY_EMAIL",
              "TI22_VIDEO_DL_STUDYGATEWAY_PASSWORD"):
    monkeypatch.delenv(var, raising=False)
  e, p, src = get_creds(_args(), "studygateway")
  assert (e, p, src) == (None, None, "missing")
  monkeypatch.setenv("TI22_VIDEO_DL_STUDYGATEWAY_EMAIL", "site@x")
  monkeypatch.setenv("TI22_VIDEO_DL_STUDYGATEWAY_PASSWORD", "sitepw")
  e, p, src = get_creds(_args(), "studygateway")
  assert (e, p, src) == ("site@x", "sitepw", "site-env")
  e, p, src = get_creds(_args(email="flag@x"), "studygateway")
  assert (e, p, src) == ("flag@x", "sitepw", "flag")


def test_get_creds_no_generic_fallback(monkeypatch):
  """Retired TI22_EMAIL/PASSWORD must not leak across sites."""
  monkeypatch.setenv("TI22_EMAIL", "gen@x")
  monkeypatch.setenv("TI22_PASSWORD", "genpw")
  e, p, src = get_creds(_args(), "studygateway")
  assert (e, p, src) == (None, None, "missing")


def test_env_creds_present_scoping(monkeypatch):
  from titanium_video_downloader.cli import _env_creds_present
  for var in ("TI22_VIDEO_DL_STUDYGATEWAY_EMAIL",
              "TI22_VIDEO_DL_VIMEO_EMAIL", "TI22_EMAIL"):
    monkeypatch.delenv(var, raising=False)
  assert _env_creds_present("studygateway") is False
  monkeypatch.setenv("TI22_EMAIL", "gen@x")
  assert _env_creds_present("studygateway") is False
  monkeypatch.setenv("TI22_VIDEO_DL_VIMEO_EMAIL", "v@v")
  assert _env_creds_present("studygateway") is False
  assert _env_creds_present("vimeo") is True
