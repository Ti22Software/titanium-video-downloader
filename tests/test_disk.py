"""Offline tests: disk preflight (faked filesystems, no I/O)."""

import pytest

from titanium_video_downloader.core import disk as disk_mod
from titanium_video_downloader.core.disk import (
  check_space,
  download_estimate,
  free_bytes,
  human_bytes,
)


def test_human_bytes_decimal():
  assert human_bytes(2_288_249_392) == "2.3 GB"
  assert human_bytes(3_000_000) == "3.0 MB"
  assert human_bytes(999) == "999 B"


def test_free_bytes_unreadable_is_none(monkeypatch):
  monkeypatch.setattr(disk_mod.shutil, "disk_usage",
                      lambda _: (_ for _ in ()).throw(OSError("NAS")))
  assert free_bytes("/mnt/nas") is None


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
  monkeypatch.setattr(disk_mod.shutil, "disk_usage", lambda _: _U())
  check_space(tmp_path, 100, "temporary files")


def test_check_space_fails_with_numbers(tmp_path, monkeypatch):
  class _U:
    free = 10
  monkeypatch.setattr(disk_mod.shutil, "disk_usage", lambda _: _U())
  with pytest.raises(SystemExit) as exc:
    check_space(tmp_path, 5_000_000_000, "output")
  assert exc.value.code == 1


def test_check_space_unreadable_warns_and_passes(tmp_path, monkeypatch, capsys):
  monkeypatch.setattr(disk_mod.shutil, "disk_usage",
                      lambda _: (_ for _ in ()).throw(OSError("cloud")))
  check_space(tmp_path, 10**12, "output")
  assert "without a space check" in capsys.readouterr().err
