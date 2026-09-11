"""Offline tests: rumble watch→embedJS resolve, muxed renditions, gates."""

import io
import tarfile

import pytest
import requests

from titanium_video_downloader.extractors import rumble as rumble_mod
from titanium_video_downloader.extractors.base import AuthError
from titanium_video_downloader.extractors.rumble import (
  _video_key,
  RumbleAuth,
  segment_headers,
)

WATCH_HTML = ('<html><head><link rel="alternate" '
              'href="https://rumble.com/embed/v778qwk/" ></head></html>')

EMBEDJS = {
  "title": "COINBASE JUST FIRED",
  "duration": 145,
  "fps": 30,
  "live": 0,
  "ua": {
    "tar": {
      "360": {"url": "https://cdn/x.baa.tar?r_file=chunklist.m3u8",
              "meta": {"bitrate": 682, "size": 12431360, "w": 204, "h": 360}},
      "1080": {"url": "https://cdn/x.haa.tar?r_file=chunklist.m3u8",
               "meta": {"bitrate": 4113, "size": 74977280, "w": 608, "h": 1080}},
    },
    "audio": {
      "192": {"url": "https://cdn/x.Gaa.aac",
              "meta": {"bitrate": 192, "size": 3498040, "w": 0, "h": 0}},
    },
  },
}

WATCH_URL = "https://rumble.com/v79fews-coinbase.html"


def _tar_bytes(uris):
  buf = io.BytesIO()
  body = "".join(f"#EXTINF:6.0,\n{u}\n" for u in uris)
  body = "#EXTM3U\n#EXT-X-VERSION:3\n" + body
  with tarfile.open(fileobj=buf, mode="w") as tf:
    info = tarfile.TarInfo("chunklist.m3u8")
    data = body.encode()
    info.size = len(data)
    tf.addfile(info, io.BytesIO(data))
  return buf.getvalue()


class _Resp:
  def __init__(self, status=200, text="", payload=None, content=b""):
    self.status_code = status
    self.text = text
    self._payload = payload
    self.content = content

  def json(self):
    if isinstance(self._payload, Exception):
      raise self._payload
    return self._payload


def _session(tar_content=None):
  class _Stub:
    def get(self, url, headers=None, timeout=None):
      if "embedJS" in url:
        return _Resp(200, payload=dict(EMBEDJS))
      if ".tar?" in url:
        return _Resp(200, content=tar_content or _tar_bytes(
          ["https://cdn/s0.ts", "seg-1.ts"]))
      return _Resp(200, text=WATCH_HTML)
  return _Stub()


def test_match():
  auth = RumbleAuth()
  assert auth.match(WATCH_URL)
  assert auth.match("https://www.rumble.com/v79fews-x.html")
  assert auth.match("https://rumble.com/embed/v778qwk/")
  assert auth.match("https://rumble.com/shorts/v7fb20s")
  assert auth.match("https://rumble.com/shorts/v7fb20s/")
  assert not auth.match("https://vimeo.com/123")
  assert not auth.match("https://rumble.com/c/somechannel")
  assert not auth.match("https://rumble.com/shorts/")
  assert RumbleAuth.requires_auth is False
  assert RumbleAuth.site_key == "rumble"


def test_video_key():
  assert _video_key(WATCH_HTML) == "v778qwk"
  assert _video_key("<html></html>") is None


def test_resolve_embed_returns_embedjs_url():
  auth = RumbleAuth()
  url = auth.resolve_embed(_session(), WATCH_URL)
  assert url.startswith("https://rumble.com/embedJS/u3/")
  assert "v=v778qwk" in url


def test_resolve_embed_shorts_returns_watch_url():
  assert RumbleAuth().resolve_embed(_session(), SHORTS_URL) == SHORTS_URL


def test_resolve_playlist_builds_muxed_renditions():
  auth = RumbleAuth()
  videos, audios, qmap, title = auth.resolve_playlist(_session(), WATCH_URL)
  assert title == "COINBASE JUST FIRED"
  assert [v["id"] for v in videos] == ["rumble-1080p", "rumble-360p"]
  assert all(v["muxed"] for v in videos)
  assert all(v["init_segment"] is None for v in videos)
  top = videos[0]
  assert (top["width"], top["height"]) == (608, 1080)
  assert top["size_total"] == 74977280
  assert qmap == {"rumble-1080p": "1080p", "rumble-360p": "360p"}
  assert len(audios) == 1
  assert audios[0]["segments"] == [{"url": "https://cdn/x.Gaa.aac", "size": 3498040}]
  # relative chunklist URIs resolve against the tar URL
  assert videos[1]["segments"][-1]["url"] == "https://cdn/seg-1.ts"


def test_resolve_playlist_gates():
  auth = RumbleAuth()

  class _Live(_Resp):
    pass

  class _LiveSession:
    def get(self, url, headers=None, timeout=None):
      if "embedJS" in url:
        return _Resp(200, payload=dict(EMBEDJS, live=1))
      return _Resp(200, text=WATCH_HTML)

  with pytest.raises(SystemExit):
    RumbleAuth().resolve_playlist(_LiveSession(), WATCH_URL)


def test_resolve_embed_no_key_anywhere_fails(monkeypatch):
  # Keyless requests HTML (login wall) falls through to the browser; only
  # a keyless browser page fails.
  monkeypatch.setattr(RumbleAuth, "_browser_bundle",
                      lambda self, session, url, key=None, debug=False:
                      (None, "<html>no key here</html>", None))
  with pytest.raises(SystemExit):
    RumbleAuth().resolve_embed(_StubNoKey(), WATCH_URL)


class _StubNoKey:
  def get(self, url, headers=None, timeout=None):
    return _Resp(200, text="<html>no key here</html>")


def test_bootstrap_falls_back_to_browser_on_403(monkeypatch):
  calls = {"watch": 0, "embedjs": 0}

  class _Gated:
    def get(self, url, headers=None, timeout=None):
      if "embedJS" in url:
        calls["embedjs"] += 1
      else:
        calls["watch"] += 1
      return _Resp(403, text="challenge")

  monkeypatch.setattr(RumbleAuth, "_browser_bundle",
                      lambda self, session, url, key=None, debug=False:
                      (key or "v778qwk", WATCH_HTML, dict(EMBEDJS)))
  auth = RumbleAuth()
  key, data = auth._bootstrap(_Gated(), WATCH_URL)
  assert key == "v778qwk"
  assert data["title"] == "COINBASE JUST FIRED"
  # second call served from cache: no further fetches
  auth._bootstrap(_Gated(), WATCH_URL)
  # watch gated → key unknown → requests embedJS untried (single page load
  # covers both sides in-page instead of a second round-trip)
  assert calls == {"watch": 1, "embedjs": 0}


def test_browser_bundle_single_load_serves_both(monkeypatch):
  loads = {"goto": 0, "content": 0, "evaluate": 0}

  class _Page:
    def goto(self, url, wait_until=None, timeout=None):
      loads["goto"] += 1

    def content(self):
      loads["content"] += 1
      return WATCH_HTML

    def evaluate(self, js, url):
      loads["evaluate"] += 1
      import json as _json
      return {"status": 200, "text": _json.dumps(dict(EMBEDJS))}

  class _Ctx:
    def new_page(self):
      return _Page()

    def cookies(self):
      return []

    def close(self):
      pass

  class _Browser:
    def new_context(self, user_agent=None):
      return _Ctx()

  import titanium_video_downloader.core.browser as browser_mod
  monkeypatch.setattr(browser_mod, "get_browser", lambda headed=False: _Browser())

  class _Bare:
    cookies = None

  auth = RumbleAuth()
  key, html, data = auth._browser_bundle(_Bare(), WATCH_URL, None, debug=False)
  assert key == "v778qwk"
  assert "embed/v778qwk" in html
  assert data["title"] == "COINBASE JUST FIRED"
  assert loads == {"goto": 1, "content": 1, "evaluate": 1}


def test_bootstrap_prefers_requests_when_open():
  auth = RumbleAuth()
  key, data = auth._bootstrap(_session(), WATCH_URL)
  assert key == "v778qwk"
  assert data["duration"] == 145


def test_login_is_noop():
  auth = RumbleAuth()
  sentinel = object()
  assert auth.login(sentinel, None, None) is sentinel
  assert segment_headers("https://rumble.com/vx")["Referer"] == "https://rumble.com/vx"


MP4_ONLY_EMBEDJS = {
  "title": "LoopOfTheWeek #1",
  "duration": 110,
  "fps": 60,
  "live": 0,
  "ua": {
    "mp4": {
      "360": {"url": "https://cdn/x.baa.mp4",
              "meta": {"bitrate": 641, "size": 8853192, "w": 640, "h": 360}},
      "2160": {"url": "https://cdn/x.jaa.mp4",
               "meta": {"bitrate": 9301, "size": 128463720, "w": 3840, "h": 2160}},
      "1080": {"url": "https://cdn/x.haa.mp4",
               "meta": {"bitrate": 4010, "size": 55383600, "w": 1920, "h": 1080}},
    },
    "audio": {
      "192": {"url": "https://cdn/x.Gaa.aac",
              "meta": {"bitrate": 194, "size": 2652673, "w": 0, "h": 0}},
    },
  },
}


def _mp4_session(payload):
  class _Stub:
    def get(self, url, headers=None, timeout=None):
      if "embedJS" in url:
        return _Resp(200, payload=dict(payload))
      return _Resp(200, text=WATCH_HTML)
  return _Stub()


def test_resolve_playlist_progressive_mp4_all_rungs():
  auth = RumbleAuth()
  videos, audios, qmap, title = auth.resolve_playlist(
    _mp4_session(MP4_ONLY_EMBEDJS), WATCH_URL)
  assert title == "LoopOfTheWeek #1"
  # all rungs exposed (incl. 2160), bitrate-desc, progressive marked
  assert [v["id"] for v in videos] == ["rumble-2160p", "rumble-1080p", "rumble-360p"]
  assert all(v["progressive"] and v["muxed"] for v in videos)
  assert all(len(v["segments"]) == 1 for v in videos)
  assert videos[0]["size_total"] == 128463720
  assert videos[0]["height"] == 2160
  assert qmap["rumble-2160p"] == "2160p"
  # audio rides inside the mp4 — empty rendition, sizes unaffected
  assert audios[0]["segments"] == []


def test_resolve_playlist_prefers_tar_over_mp4():
  both = dict(MP4_ONLY_EMBEDJS)
  both["ua"] = dict(MP4_ONLY_EMBEDJS["ua"], tar={
    "360": {"url": "https://cdn/x.baa.tar?r_file=chunklist.m3u8",
            "meta": {"bitrate": 682, "size": 12431360, "w": 204, "h": 360}},
  })
  auth = RumbleAuth()

  class _Stub:
    def get(self, url, headers=None, timeout=None):
      if "embedJS" in url:
        return _Resp(200, payload=dict(both))
      if ".tar?" in url:
        return _Resp(200, content=_tar_bytes(["https://cdn/s0.ts"]))
      return _Resp(200, text=WATCH_HTML)

  videos, audios, _qmap, _title = auth.resolve_playlist(_Stub(), WATCH_URL)
  assert [v["id"] for v in videos] == ["rumble-360p"]
  assert not videos[0].get("progressive")
  assert audios[0]["segments"] != []


def test_resolve_playlist_empty_shape_reports_keys(capsys):
  empty = {"title": "x", "duration": 1, "live": 0, "ua": {"mp4": {}, "tar": {}}}
  with pytest.raises(SystemExit):
    RumbleAuth().resolve_playlist(_mp4_session(empty), WATCH_URL)
  err = capsys.readouterr().err
  assert "tar_keys=[]" in err and "mp4_keys=[]" in err and "ua_keys=" in err


SHORTS_URL = "https://rumble.com/shorts/v7fb20s"

SHORTS_HTML = ('<html><head><meta property=og:title content="Party"></head>'
               '<body><script type="application/json">{"items": ['
               '{"relative_url": "/shorts/v7fb20s", "permalink_id": "v7fb20s",'
               ' "title": "Main Short", "duration": 44, "live": false,'
               ' "video_width": 960, "video_height": 960,'
               ' "videos": ['
               '{"url": "https://cdn/m.gaa.mp4", "type": "mp4", "res": 720,'
               ' "bitrate_kbps": 2063},'
               '{"url": "https://cdn/m.baa.mp4", "type": "mp4", "res": 360,'
               ' "bitrate_kbps": 500}]},'
               '{"relative_url": "/shorts/v7f3392", "permalink_id": "v7f3392",'
               ' "title": "Related", "duration": 30, "live": false,'
               ' "video_width": 640, "video_height": 640,'
               ' "videos": ['
               '{"url": "https://cdn/r.haa.mp4", "type": "mp4", "res": 1080,'
               ' "bitrate_kbps": 4000}]}'
               ']}</script></body></html>')


class _HeadResp:
  def __init__(self, status=200, length=0):
    self.status_code = status
    self.headers = {"Content-Length": str(length)}


def _shorts_session(lengths=None):
  lengths = lengths or {}

  class _Stub:
    def get(self, url, headers=None, timeout=None):
      return _Resp(200, text=SHORTS_HTML)

    def head(self, url, headers=None, timeout=None):
      return _HeadResp(200, lengths.get(url, 0))
  return _Stub()


def test_resolve_shorts_scopes_to_main_and_sizes_via_head():
  auth = RumbleAuth()
  videos, audios, qmap, title = auth.resolve_playlist(
    _shorts_session({"https://cdn/m.gaa.mp4": 1000,
                     "https://cdn/m.baa.mp4": 500}), SHORTS_URL)
  assert title == "Main Short"
  # main rungs only (related 1080p excluded), all rungs, progressive
  assert [v["id"] for v in videos] == ["rumble-720p", "rumble-360p"]
  assert all(v["progressive"] and v["muxed"] for v in videos)
  assert all(len(v["segments"]) == 1 for v in videos)
  assert videos[0]["size_total"] == 1000
  assert videos[0]["height"] == 720
  assert videos[0]["bitrate"] == 2063000
  assert qmap == {"rumble-720p": "720p", "rumble-360p": "360p"}
  assert audios[0]["segments"] == []


def test_resolve_shorts_source_fallback_single_rung():
  html = ('<html><body><video>'
          '<source type="video/mp4" src="https://cdn/s.gaa.mp4"/>'
          '</video></body></html>')

  class _Stub:
    def get(self, url, headers=None, timeout=None):
      return _Resp(200, text=html)

    def head(self, url, headers=None, timeout=None):
      return _HeadResp(200, 42)

  videos, _audios, qmap, _title = RumbleAuth().resolve_playlist(_Stub(), SHORTS_URL)
  assert [v["id"] for v in videos] == ["rumble-720p"]
  assert videos[0]["size_total"] == 42
  assert qmap == {"rumble-720p": "720p"}


def test_resolve_shorts_no_media_reports_page(capsys):
  class _Stub:
    def get(self, url, headers=None, timeout=None):
      return _Resp(200, text="<html><body>gone</body></html>")
  with pytest.raises(SystemExit):
    RumbleAuth().resolve_playlist(_Stub(), SHORTS_URL)
  assert "page='v7fb20s'" in capsys.readouterr().err
