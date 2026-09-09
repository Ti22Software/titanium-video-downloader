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
