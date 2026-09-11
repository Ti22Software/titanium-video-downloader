"""Offline tests: HLS chunklist reader (tar-wrapped and plain)."""

import io
import tarfile

import pytest

from titanium_video_downloader.core.fetcher import fetch_hls_chunklist

M3U8 = ("#EXTM3U\n#EXT-X-VERSION:3\n"
        "#EXTINF:6.0,\nhttps://cdn/s0.ts\n"
        "#EXTINF:6.0,\nseg-1.ts\n")


def _tar_bytes(text):
  buf = io.BytesIO()
  with tarfile.open(fileobj=buf, mode="w") as tf:
    info = tarfile.TarInfo("chunklist.m3u8")
    data = text.encode()
    info.size = len(data)
    tf.addfile(info, io.BytesIO(data))
  return buf.getvalue()


class _Resp:
  def __init__(self, status=200, content=b""):
    self.status_code = status
    self.content = content


def _session(content):
  class _Stub:
    def get(self, url, headers=None, timeout=None):
      return _Resp(200, content)
  return _Stub()


TAR_URL = "https://cdn/x.haa.tar?r_file=chunklist.m3u8"


def test_plain_m3u8_passthrough():
  uris = fetch_hls_chunklist(_session(M3U8.encode()), TAR_URL, "https://rumble.com/vx")
  assert uris == ["https://cdn/s0.ts", "https://cdn/seg-1.ts"]


def test_tar_wrapped_m3u8_unpacked():
  uris = fetch_hls_chunklist(_session(_tar_bytes(M3U8)), TAR_URL, "https://rumble.com/vx")
  assert uris == ["https://cdn/s0.ts", "https://cdn/seg-1.ts"]


def test_empty_chunklist_fails():
  with pytest.raises(SystemExit):
    fetch_hls_chunklist(_session(b"#EXTM3U\n"), TAR_URL, "https://rumble.com/vx")


def test_garbage_tar_fails():
  with pytest.raises(SystemExit):
    fetch_hls_chunklist(_session(b"not-a-tar-at-all"), TAR_URL, "https://rumble.com/vx")


def test_http_error_fails():
  class _Bad:
    def get(self, url, headers=None, timeout=None):
      return _Resp(404, b"")

  with pytest.raises(SystemExit):
    fetch_hls_chunklist(_Bad(), TAR_URL, "https://rumble.com/vx")


def test_part_write_enospc_fails_clean(tmp_path, monkeypatch, capsys):
  import errno
  from pathlib import Path
  from titanium_video_downloader.core.fetcher import download_rendition

  class _RespSeg:
    status_code = 200

    def __init__(self, data):
      self.content = data

    def raise_for_status(self):
      pass

  class _Stub:
    def get(self, url, headers=None, timeout=None):
      return _RespSeg(b"segdata")

  def _boom(self, data):
    raise OSError(errno.ENOSPC, "No space left on device")

  monkeypatch.setattr(Path, "write_bytes", _boom)
  rendition = {"init_segment": None, "segments": [{}, {}]}
  with pytest.raises(SystemExit):
    download_rendition("video", rendition, ["https://cdn/0.ts", "https://cdn/1.ts"],
                       tmp_path, _Stub(), 1, suffix=".ts", assemble=False)
  assert "disk full during video segment" in capsys.readouterr().err


def _seg_stub(n):
  class _RespSeg:
    status_code = 200

    def __init__(self, data):
      self.content = data

    def raise_for_status(self):
      pass

  class _Stub:
    def get(self, url, headers=None, timeout=None):
      return _RespSeg(b"x")

  return _Stub()


def test_manifest_flushed_on_midrun_failure(tmp_path):
  """The user's incident: ENOSPC on segment 3 of 5 must still record 0-2."""
  import errno
  import json
  from pathlib import Path
  from titanium_video_downloader.core.fetcher import download_rendition

  real_write = Path.write_bytes
  calls = {"n": 0}

  def _boom_third(self, data):
    calls["n"] += 1
    if calls["n"] >= 3:
      raise OSError(errno.ENOSPC, "No space left on device")
    return real_write(self, data)

  import pytest as _pt
  from unittest import mock
  with mock.patch.object(Path, "write_bytes", _boom_third):
    with _pt.raises(SystemExit):
      download_rendition("video", {"init_segment": None,
                                   "segments": [{}, {}, {}, {}, {}]},
                         [f"https://cdn/{i}.ts" for i in range(5)],
                         tmp_path, _seg_stub(5), 1, suffix=".ts",
                         assemble=False)
  saved = json.loads((tmp_path / "video.manifest.json").read_text())
  assert saved == [0, 1]


def test_manifest_checkpoint_cadence(tmp_path, monkeypatch):
  """MANIFEST_EVERY completions trigger a mid-run flush, not just the end."""
  import json
  from titanium_video_downloader.core import fetcher as fetcher_mod
  from titanium_video_downloader.core.fetcher import download_rendition

  writes = []
  real_write_text = fetcher_mod.Path.write_text

  def _counting(self, data, *a, **k):
    if self.name.endswith(".manifest.json"):
      writes.append(len(json.loads(data)))
    return real_write_text(self, data, *a, **k)

  monkeypatch.setattr(fetcher_mod.Path, "write_text", _counting)
  n = fetcher_mod.MANIFEST_EVERY * 2 + 5
  download_rendition("video", {"init_segment": None,
                               "segments": [{} for _ in range(n)]},
                     [f"https://cdn/{i}.ts" for i in range(n)],
                     tmp_path, _seg_stub(n), 4, suffix=".ts", assemble=False)
  assert writes[0] == fetcher_mod.MANIFEST_EVERY
  assert writes[-1] == n
  assert len(writes) == 3


def test_download_no_assemble_returns_parts_dir(tmp_path):
  from titanium_video_downloader.core.fetcher import download_rendition

  class _RespSeg:
    status_code = 200

    def __init__(self, data):
      self.content = data

    def raise_for_status(self):
      pass

  class _Stub:
    def get(self, url, headers=None, timeout=None):
      return _RespSeg(b"segdata")

  rendition = {"init_segment": "aGk=", "segments": [{}, {}]}
  out = download_rendition("video", rendition, ["https://cdn/0.ts", "https://cdn/1.ts"],
                           tmp_path, _Stub(), 2, suffix=".ts", assemble=False)
  assert out == tmp_path / "video"
  assert sorted(p.name for p in (tmp_path / "video").glob("*.ts")) == \
    ["00000.ts", "00001.ts"]
  assert not (tmp_path / "video.mp4").exists()


def test_download_assembles_by_default(tmp_path):
  from titanium_video_downloader.core.fetcher import download_rendition

  class _RespSeg:
    status_code = 200

    def __init__(self, data):
      self.content = data

    def raise_for_status(self):
      pass

  class _Stub:
    def get(self, url, headers=None, timeout=None):
      return _RespSeg(b"segdata")

  rendition = {"init_segment": None, "segments": [{}, {}]}
  out = download_rendition("video", rendition, ["https://cdn/0.ts", "https://cdn/1.ts"],
                           tmp_path, _Stub(), 2, suffix=".ts")
  assert out == tmp_path / "video.mp4"
  assert out.read_bytes() == b"segdatasegdata"
