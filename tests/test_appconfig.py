"""Tests: config.toml layering (CLI > file > builtins), leniency, pick."""

import argparse
import io
import json
from contextlib import redirect_stderr

import pytest

from titanium_video_downloader.core import appconfig as ac


def _args(**over):
  ns = argparse.Namespace(**{k: None for k in list(ac.BUILTINS) + ["pick", "no_pick"]})
  for k, v in over.items():
    setattr(ns, k, v)
  return ns


def test_cli_beats_config_beats_builtin():
  args, sources = ac.resolve_config(
    _args(quality="720p"), file_pairs={"quality": "480p", "concurrency": 8})
  assert args.quality == "720p" and sources["quality"] == "cli"
  assert args.concurrency == 8 and sources["concurrency"] == "config"
  assert args.on_missing_quality == "fallback"
  assert sources["on_missing_quality"] == "builtin:fallback"


def test_unknown_key_bad_type_bad_choice_ignored(capsys):
  args, _sources = ac.resolve_config(_args(), file_pairs={
    "bogus": 1, "concurrency": "lots", "quality": 720,
    "on_missing_quality": "sometimes", "no_auth": "yes"})
  assert args.concurrency == 4
  assert args.quality == "best"
  assert args.on_missing_quality == "fallback"
  assert args.no_auth is False
  err = capsys.readouterr().err
  assert "bogus" in err and "concurrency" in err and "on_missing_quality" in err


def test_malformed_toml_notes_and_yields_empty(tmp_path, capsys):
  bad = tmp_path / "config.toml"
  bad.write_text("quality = [unclosed\n")
  pairs, found = ac.load_config_file(bad)
  assert pairs == {} and found is None
  assert "malformed" in capsys.readouterr().err


def test_missing_file_is_silent(tmp_path, capsys):
  pairs, found = ac.load_config_file(tmp_path / "nope.toml")
  assert pairs == {} and found is None
  assert capsys.readouterr().err == ""


def test_pick_tristate():
  a, s = ac.resolve_config(_args(), file_pairs={})
  assert a.pick is False and s["pick"] == "builtin:False"
  a, s = ac.resolve_config(_args(), file_pairs={"pick_always": True})
  assert a.pick is True and s["pick"] == "config:pick_always=True"
  a, s = ac.resolve_config(_args(no_pick=True), file_pairs={"pick_always": True})
  assert a.pick is False and s["pick"] == "cli:--no-pick"
  a, s = ac.resolve_config(_args(pick=True), file_pairs={})
  assert a.pick is True and s["pick"] == "cli:--pick"


def test_print_config_end_to_end(tmp_path, capsys):
  cfg = tmp_path / "config.toml"
  cfg.write_text('quality = "480p"\nconcurrency = 8\n')
  from titanium_video_downloader.cli import main
  rc = main(["--config", str(cfg), "--print-config", "--quality", "720p"])
  assert rc == 0
  doc = json.loads(capsys.readouterr().out)
  assert doc["values"]["quality"] == "720p"
  assert doc["sources"]["quality"] == "cli"
  assert doc["values"]["concurrency"] == 8
  assert doc["sources"]["concurrency"] == "config"
  assert doc["values"]["pick"] is False
  assert "email" not in doc["values"] and "password" not in doc["values"]
  assert doc["config_file"] == str(cfg)


def test_pick_mutex_rejected(capsys):
  from titanium_video_downloader.cli import main
  with pytest.raises(SystemExit) as ei:
    main(["--pick", "--no-pick", "https://vimeo.com/1"])
  assert ei.value.code == 2


def test_blank_config_value_falls_through_with_note(capsys):
  args, sources = ac.resolve_config(
    _args(), file_pairs={"quality": "", "output_dir": "   "})
  assert args.quality == "best" and sources["quality"] == "builtin:best"
  assert args.output_dir is None
  err = capsys.readouterr().err
  assert "empty value" in err and "'quality'" in err


def test_blank_cli_flag_fails_like_bad_flag(capsys):
  from titanium_video_downloader.cli import main
  with pytest.raises(SystemExit) as ei:
    main(["--quality", "", "https://vimeo.com/1"])
  assert ei.value.code == 2
  assert "--quality needs a non-empty value" in capsys.readouterr().err
  with pytest.raises(SystemExit) as ei:
    main(["--output-dir", "  ", "https://vimeo.com/1"])
  assert ei.value.code == 2
