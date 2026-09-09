"""Offline tests: OTTData/config/playlist parsing."""

import pytest

from titanium_downloader.core.models import parse_playlist
from titanium_downloader.extractors.studygateway import extract_ottdata, pick_playlist_url


def test_extract_ottdata_ok():
  html = ('<script>window.OTTData = {"config_url": "https://x/config", '
          '"video": {"title": "T", "id": 7}};</script>')
  assert extract_ottdata(html) == ("https://x/config", "T", 7)


def test_extract_ottdata_missing():
  assert extract_ottdata("<html>no marker</html>") == (None, None, None)


def test_pick_playlist_url_primary_and_fallback():
  config = {"request": {"files": {"dash": {
    "default_cdn": "ak",
    "cdns": {"ak": {"avc_url": "https://cdn/a"}, "fb": {"avc_url": "https://cdn/b"}},
    "streams_avc": [{"id": "v1", "quality": "1080p"}],
  }}}}
  primary, fallback, qmap = pick_playlist_url(config)
  assert primary == "https://cdn/a"
  assert fallback == "https://cdn/b"
  assert qmap == {"v1": "1080p"}


def test_pick_playlist_url_missing_dash_exits():
  with pytest.raises(SystemExit):
    pick_playlist_url({"request": {"files": {}}})


def test_parse_playlist_minimal():
  pl = {
    "base_url": "https://cdn/base/",
    "video": [{"id": "v1", "width": 1280, "height": 720, "bitrate": 100,
               "codecs": "avc1", "duration": 10.0, "framerate": 23.98,
               "init_segment": "aGk=", "segments": [{"url": "s0"}]}],
    "audio": [{"id": "a1", "bitrate": 128000, "codecs": "mp4a.40.2",
               "init_segment": "aGk=", "segments": [{"url": "s0"}]}],
  }
  videos, audios = parse_playlist(pl)
  assert [v["id"] for v in videos] == ["v1"]
  assert [a["id"] for a in audios] == ["a1"]


def test_parse_playlist_empty_exits():
  with pytest.raises(SystemExit):
    parse_playlist({"video": [], "audio": []})
