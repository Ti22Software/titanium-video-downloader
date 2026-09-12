"""Offline tests: ffmpeg_path() precedence (no real binaries executed)."""

import os
import subprocess
import sys

import pytest

from titanium_video_downloader.core import paths
from titanium_video_downloader.core.paths import (
  _bundled_ffmpeg,
  config_relative_path,
  ffmpeg_path,
)


def _exe(tmp_path, name="ff"):
  # Windows executability is extension-based (shebang content can never
  # execute natively there), so fixtures wear .exe on nt to mean the same
  # thing 0o755 means on POSIX.
  if os.name == "nt" and not name.lower().endswith(".exe"):
    name += ".exe"
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
  from titanium_video_downloader.core import mux as mux_mod
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
  from titanium_video_downloader.core import mux as mux_mod
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


def test_remux_concat_list_is_absolute(tmp_path, monkeypatch):
  """Concat entries must be absolute: the demuxer resolves relatives
  against the list file's own dir, doubling relative workdir paths."""
  from titanium_video_downloader.core import mux as mux_mod
  parts = tmp_path / "video"
  parts.mkdir()
  (parts / "00000.ts").write_bytes(b"fake")

  class _Proc:
    returncode = 0
    stderr = ""

  monkeypatch.setattr(mux_mod.subprocess, "run", lambda *a, **k: _Proc())
  monkeypatch.setattr(mux_mod.shutil, "which", lambda _: None)
  monkeypatch.setattr(mux_mod, "ffmpeg_path", lambda explicit=None: "/bundled/ffmpeg")
  mux_mod.remux_concat(parts, tmp_path / "out.mp4")
  lines = (parts / "concat.txt").read_text().splitlines()
  assert len(lines) == 1 and lines[0].startswith("file '") and lines[0].endswith("'")
  assert os.path.isabs(lines[0][len("file '"):-1])


def test_remux_retry_note_only_between_attempts(tmp_path, monkeypatch, capsys):
  from titanium_video_downloader.core import mux as mux_mod
  parts = tmp_path / "video"
  parts.mkdir()
  (parts / "00000.ts").write_bytes(b"fake")

  class _Proc:
    def __init__(self, rc):
      self.returncode = rc
      self.stderr = "boom" if rc else ""

  def _run(cmd, **kw):
    _run.n += 1
    return _Proc(1) if _run.n == 1 else _Proc(0)
  _run.n = 0

  monkeypatch.setattr(mux_mod.subprocess, "run", _run)
  monkeypatch.setattr(mux_mod.shutil, "which", lambda _: "/usr/bin/ffmpeg")
  monkeypatch.setattr(mux_mod, "ffmpeg_path", lambda explicit=None: "/bundled/ffmpeg")
  mux_mod.remux_concat(parts, tmp_path / "out.mp4")
  err = capsys.readouterr().err
  assert err.count("retrying with system ffmpeg") == 1


def test_remux_final_failure_prints_no_retry_note(tmp_path, monkeypatch, capsys):
  from titanium_video_downloader.core import mux as mux_mod
  parts = tmp_path / "video"
  parts.mkdir()
  (parts / "00000.ts").write_bytes(b"fake")

  class _Proc:
    returncode = 1
    stderr = "tail-err"

  monkeypatch.setattr(mux_mod.subprocess, "run", lambda *a, **k: _Proc())
  monkeypatch.setattr(mux_mod.shutil, "which", lambda _: "/usr/bin/ffmpeg")
  monkeypatch.setattr(mux_mod, "ffmpeg_path", lambda explicit=None: "/bundled/ffmpeg")
  with pytest.raises(SystemExit):
    mux_mod.remux_concat(parts, tmp_path / "out.mp4")
  err = capsys.readouterr().err
  assert err.count("retrying with system ffmpeg") == 1
  assert "tail-err" in err


def test_ffmpeg_error_no_space_names_disk(capsys):
  from titanium_video_downloader.core.mux import _ffmpeg_error
  with pytest.raises(SystemExit):
    _ffmpeg_error("mux", "[out#0] Error opening output: No space left on device")
  err = capsys.readouterr().err
  assert "disk full during ffmpeg mux" in err
  assert "re-run to resume" in err
  assert err.count("error:") == 1


def test_ffmpeg_error_trailer_eio_names_io_with_tail(capsys):
  from titanium_video_downloader.core.mux import _ffmpeg_error
  body = ("[out#0/mp4] Error writing trailer: Input/output error\n"
          "[out#0/mp4] Error closing file: Input/output error")
  with pytest.raises(SystemExit):
    _ffmpeg_error("mux", body)
  err = capsys.readouterr().err
  assert "disk I/O error" in err
  assert "Error writing trailer" in err  # raw tail preserved for forensics


def test_ffmpeg_error_generic_unchanged(capsys):
  from titanium_video_downloader.core.mux import _ffmpeg_error
  with pytest.raises(SystemExit):
    _ffmpeg_error("remux", "Something unexpected: bad option")
  err = capsys.readouterr().err
  assert "error: ffmpeg remux failed." in err
  assert "bad option" in err


def test_mux_detaches_stdin(tmp_path, monkeypatch):
  """ffmpeg must never hold the user's tty: a crash would otherwise leave
  echo disabled (invisible typing until `reset`)."""
  from titanium_video_downloader.core import mux as mux_mod
  seen = {}

  class _Out:
    def __iter__(self):
      return iter([])

    def close(self):
      pass

  class _Err:
    def read(self):
      return ""

    def close(self):
      pass

  class _Proc:
    def __init__(self, **kw):
      seen.update(kw)
      self.stdout = _Out()
      self.stderr = _Err()

    def wait(self):
      return 0

      monkeypatch.setattr(mux_mod, "Popen", lambda *a, **k: _Proc(**k))
      mux_mod.mux(tmp_path / "v.mp4", tmp_path / "a.m4a", tmp_path / "out.mp4")
      assert seen.get("stdin") == subprocess.DEVNULL


def test_remux_detaches_stdin(tmp_path, monkeypatch):
  from titanium_video_downloader.core import mux as mux_mod
  parts = tmp_path / "video"
  parts.mkdir()
  (parts / "00000.ts").write_bytes(b"fake")
  seen = {}

  class _Proc:
    returncode = 0
    stderr = ""

  def _run(cmd, **kw):
    seen.update(kw)
    return _Proc()

  monkeypatch.setattr(mux_mod.subprocess, "run", _run)
  monkeypatch.setattr(mux_mod.shutil, "which", lambda _: None)
  monkeypatch.setattr(mux_mod, "ffmpeg_path", lambda explicit=None: "/bundled/ffmpeg")
  mux_mod.remux_concat(parts, tmp_path / "out.mp4")
  assert seen.get("stdin") == subprocess.DEVNULL


def test_is_executable_platform_aware(monkeypatch, tmp_path):
  """POSIX honors exec bits; Windows (no exec bits) honors PATHEXT."""
  from titanium_video_downloader.core.paths import _is_executable
  import sys as _sys
  assert _is_executable(_sys.executable) is True
  monkeypatch.setattr(os, "name", "nt")
  assert _is_executable(tmp_path / "ff.exe") is True
  assert _is_executable(tmp_path / "nox") is False
  monkeypatch.delenv("PATHEXT", raising=False)
  assert _is_executable(tmp_path / "ff.CMD") is True


def test_bundled_resolves_without_fakes():
  """Guards the real bundled-binary resolution with zero monkeypatching:
  the exact path test_mux_detaches_stdin depends on. Skips (not fails)
  where the ffmpeg extra is absent."""
  imageio_ffmpeg = pytest.importorskip("imageio_ffmpeg")
  from titanium_video_downloader.core.paths import _bundled_ffmpeg
  exe = imageio_ffmpeg.get_ffmpeg_exe()
  found = _bundled_ffmpeg()
  assert found is not None, (
    f"bundled resolution failed: exe={exe!r} "
    f"isfile={os.path.isfile(exe)} pathext={os.environ.get('PATHEXT')!r}")
  assert found == exe


def test_config_relative_absolute_passthrough(tmp_path):
  target = tmp_path / "c.txt"
  assert config_relative_path(str(target)) == target
  assert config_relative_path(None) is None


def test_config_relative_tilde_expands(monkeypatch, tmp_path):
  """~ follows the platform's home variable (HOME on POSIX, USERPROFILE
  on Windows — this interpreter honors the latter)."""
  monkeypatch.setenv("HOME", str(tmp_path))
  if os.name == "nt":
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
  assert config_relative_path("~/c.txt") == tmp_path / "c.txt"


def test_config_relative_cwd_wins(monkeypatch, tmp_path):
  work = tmp_path / "work"
  work.mkdir()
  (work / "c.txt").write_bytes(b"x")
  cfg = tmp_path / "cfg"
  cfg.mkdir()
  (cfg / "c.txt").write_bytes(b"y")
  monkeypatch.chdir(work)
  monkeypatch.setenv("TI22_VIDEO_DL_CONFIG_DIR", str(cfg))
  assert config_relative_path("c.txt") == work / "c.txt"


def test_config_relative_falls_back_to_config_dir(monkeypatch, tmp_path, capsys):
  work = tmp_path / "work"
  work.mkdir()
  cfg = tmp_path / "cfg"
  cfg.mkdir()
  monkeypatch.chdir(work)
  monkeypatch.setenv("TI22_VIDEO_DL_CONFIG_DIR", str(cfg))
  out = config_relative_path("c.txt")
  assert out == cfg / "c.txt"
  assert "config dir" in capsys.readouterr().err
