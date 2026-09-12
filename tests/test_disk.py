"""Offline tests: disk preflight + clean write failures (no real I/O)."""

import errno

import pytest

from titanium_video_downloader.core import disk as disk_mod
from titanium_video_downloader.core.disk import (
  check_space,
  download_estimate,
  free_bytes,
  human_bytes,
  io_fail,
)


def test_human_bytes_decimal():
  assert human_bytes(2_288_249_392) == "2.3 GB"
  assert human_bytes(3_000_000) == "3.0 MB"
  assert human_bytes(999) == "999 B"


def test_free_bytes_unreadable_is_none(monkeypatch):
  monkeypatch.setattr(disk_mod.shutil, "disk_usage",
                      lambda _: (_ for _ in ()).throw(OSError("NAS")))
  monkeypatch.setattr(disk_mod.os, "statvfs",
                      lambda _: (_ for _ in ()).throw(OSError("NAS")),
                      raising=False)
  assert free_bytes("/mnt/nas") is None


def test_free_bytes_prefers_bavail_over_free(monkeypatch):
  """Reserved-block gap: f_bfree includes root-reserved blocks, f_bavail
  (what df shows, what unprivileged users can use) does not."""
  class _Stat:
    f_bavail = 551_000_000
    f_bfree = 577_000_000
    f_frsize = 1

  seen = {}

  def _du(path):
    seen["shutil_called"] = True
    raise AssertionError("shutil must not run when statvfs works")

  monkeypatch.setattr(disk_mod.os, "statvfs", lambda _: _Stat(),
                      raising=False)
  monkeypatch.setattr(disk_mod.shutil, "disk_usage", _du)
  assert free_bytes("/mnt/f") == 551_000_000
  assert seen == {}


def test_free_bytes_windows_fallback_no_statvfs(monkeypatch):
  """os.statvfs doesn't exist on Windows → shutil fallback, still usable."""
  monkeypatch.delattr(disk_mod.os, "statvfs", raising=False)

  class _U:
    free = 123_456

  monkeypatch.setattr(disk_mod.shutil, "disk_usage", lambda _: _U())
  assert free_bytes("C:\\videos") == 123_456


def test_download_estimate_sums_legs():
  video = {"segments": [{"size": 100}, {"size": 50}]}
  audio = {"segments": [{"size": 25}]}
  assert download_estimate(video, audio) == 175


def test_download_estimate_prefers_size_total():
  video = {"size_total": 1000, "segments": [{"size": 1}]}
  assert download_estimate(video, {"segments": []}) == 1000


def test_check_space_passes(tmp_path, monkeypatch):
  class _U:
    free = 10_000_000_000
  monkeypatch.setattr(disk_mod.os, "statvfs",
                      lambda _: (_ for _ in ()).throw(OSError("no stat")),
                      raising=False)
  monkeypatch.setattr(disk_mod.shutil, "disk_usage", lambda _: _U())
  check_space(tmp_path, 100, "temporary files")


def test_check_space_fails_with_numbers(tmp_path, monkeypatch):
  class _U:
    free = 10
  monkeypatch.setattr(disk_mod.os, "statvfs",
                      lambda _: (_ for _ in ()).throw(OSError("no stat")),
                      raising=False)
  monkeypatch.setattr(disk_mod.shutil, "disk_usage", lambda _: _U())
  with pytest.raises(SystemExit) as exc:
    check_space(tmp_path, 5_000_000_000, "output")
  assert exc.value.code == 1


def test_check_space_unreadable_warns_and_passes(tmp_path, monkeypatch, capsys):
  monkeypatch.setattr(disk_mod.os, "statvfs",
                      lambda _: (_ for _ in ()).throw(OSError("cloud")),
                      raising=False)
  monkeypatch.setattr(disk_mod.shutil, "disk_usage",
                      lambda _: (_ for _ in ()).throw(OSError("cloud")))
  check_space(tmp_path, 10**12, "output")
  assert "without a space check" in capsys.readouterr().err


def test_io_fail_disk_full_names_operation(capsys):
  with pytest.raises(SystemExit):
    io_fail("video segment 3 write", OSError(errno.ENOSPC, "No space left"))
  err = capsys.readouterr().err
  assert "disk full during video segment 3 write" in err
  assert "re-run to resume" in err


def test_io_fail_permission_names_operation(capsys):
  with pytest.raises(SystemExit):
    io_fail("video.mp4 assembly", OSError(errno.EACCES, "Permission denied", "/x/y"))
  err = capsys.readouterr().err
  assert "cannot write during video.mp4 assembly" in err
  assert "permissions" in err


def test_io_fail_generic_single_line(capsys):
  with pytest.raises(SystemExit):
    io_fail("manifest write", OSError(errno.EIO, "I/O error"))
  lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("error:")]
  assert len(lines) == 1
  assert "manifest write" in lines[0]
