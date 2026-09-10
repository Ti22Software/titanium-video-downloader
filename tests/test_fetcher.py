"""Offline tests: HLS chunklist reader (tar-wrapped and plain)."""

import io
import tarfile

import pytest

from titanium_downloader.core.fetcher import fetch_hls_chunklist

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
