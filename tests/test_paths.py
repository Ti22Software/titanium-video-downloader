"""Offline tests: ffmpeg_path() precedence (no real binaries executed)."""

import os
import sys

import pytest

from titanium_downloader.core import paths
from titanium_downloader.core.paths import _bundled_ffmpeg, ffmpeg_path


def _exe(tmp_path, name="ff"):
  exe = tmp_path / name
  exe.write_bytes(b"#!/bin/sh\n")
  exe.chmod(0o755)
  return str(exe)


def test_explicit_wins(tmp_path, monkeypatch):
  monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
  monkeypatch.setattr(paths.shutil, "which", lambda _: "/usr/bin/ffmpeg")
  assert ffmpeg_path(_exe(tmp_path)) == _exe(tmp_path)


def test_explicit_missing_or_not_executable_fails(tmp_path):
  with pytest.raises(SystemExit):
    ffmpeg_path(str(tmp_path / "nope"))
  noexec = tmp_path / "nox"
  noexec.write_bytes(b"x")
  noexec.chmod(0o644)
  with pytest.raises(SystemExit):
    ffmpeg_path(str(noexec))


def test_bundled_beats_path(monkeypatch, tmp_path):
  exe = _exe(tmp_path, "bundled")

  class _Fake:
    @staticmethod
    def get_ffmpeg_exe():
      return exe

  monkeypatch.setitem(sys.modules, "imageio_ffmpeg", _Fake)
  monkeypatch.setattr(paths.shutil, "which", lambda _: "/usr/bin/ffmpeg")
  assert ffmpeg_path() == exe


def test_falls_back_to_path(monkeypatch):
  monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
  monkeypatch.setattr(paths.shutil, "which", lambda _: "/usr/bin/ffmpeg")
  assert ffmpeg_path() == "/usr/bin/ffmpeg"
  assert _bundled_ffmpeg() is None


def test_nothing_found_fails(monkeypatch):
  monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
  monkeypatch.setattr(paths.shutil, "which", lambda _: None)
  with pytest.raises(SystemExit):
    ffmpeg_path()
  assert os.name in ("posix", "nt")  # documents supported platforms


def test_remux_falls_back_to_system(tmp_path, monkeypatch):
  from titanium_downloader.core import mux as mux_mod
  parts = tmp_path / "video"
  parts.mkdir()
  (parts / "00000.ts").write_bytes(b"fake")
  calls = []

  class _Proc:
    def __init__(self, rc):
      self.returncode = rc
      self.stderr = "boom" if rc else ""

  def _run(cmd, **kw):
    calls.append(cmd[0])
    return _Proc(-11) if len(calls) == 1 else _Proc(0)

  monkeypatch.setattr(mux_mod.subprocess, "run", _run)
  monkeypatch.setattr(mux_mod.shutil, "which", lambda _: "/usr/bin/ffmpeg")
  monkeypatch.setattr(mux_mod, "ffmpeg_path", lambda explicit=None: "/bundled/ffmpeg")
  out = mux_mod.remux_concat(parts, tmp_path / "out.mp4")
  assert out == tmp_path / "out.mp4"
  assert calls == ["/bundled/ffmpeg", "/usr/bin/ffmpeg"]


def test_remux_all_fail_exits(tmp_path, monkeypatch):
  from titanium_downloader.core import mux as mux_mod
  parts = tmp_path / "video"
  parts.mkdir()
  (parts / "00000.ts").write_bytes(b"fake")

  class _Proc:
    returncode = 1
    stderr = "nope"

  monkeypatch.setattr(mux_mod.subprocess, "run", lambda *a, **k: _Proc())
  monkeypatch.setattr(mux_mod.shutil, "which", lambda _: None)
  monkeypatch.setattr(mux_mod, "ffmpeg_path", lambda explicit=None: "/bundled/ffmpeg")
  with pytest.raises(SystemExit):
    mux_mod.remux_concat(parts, tmp_path / "out.mp4")
