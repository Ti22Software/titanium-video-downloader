"""Offline tests: rendition selection (fail() surfaces as SystemExit)."""

import pytest

from titanium_downloader.core.models import label_for, select_audio, select_video

VIDEOS = [
  {"id": "v1080", "width": 1920, "height": 1080, "bitrate": 5000},
  {"id": "v720", "width": 1280, "height": 720, "bitrate": 2500},
  {"id": "v360", "width": 640, "height": 360, "bitrate": 800},
]


def test_select_best_is_highest_bitrate():
  assert select_video(VIDEOS, "best")["id"] == "v1080"
  assert select_video(VIDEOS, "max")["id"] == "v1080"


def test_select_by_height_label():
  assert select_video(VIDEOS, "720p")["id"] == "v720"
  assert select_video(VIDEOS, "360")["id"] == "v360"


def test_select_by_id_prefix():
  assert select_video(VIDEOS, "v72")["id"] == "v720"


def test_select_unknown_quality_exits():
  with pytest.raises(SystemExit):
    select_video(VIDEOS, "2160p")


def test_label_for_height_and_fallback():
  assert label_for(VIDEOS[0]) == "1080p"
  assert label_for({"id": "abcdef123456"}) == "abcdef12"


def test_select_audio_prefers_aac():
  audios = [
    {"id": "a-opus", "bitrate": 160000, "codecs": "opus"},
    {"id": "a-aac", "bitrate": 128000, "codecs": "mp4a.40.2"},
  ]
  assert select_audio(audios)["id"] == "a-aac"


def test_select_audio_falls_back_to_highest():
  audios = [
    {"id": "a1", "bitrate": 64000, "codecs": "opus"},
    {"id": "a2", "bitrate": 96000, "codecs": "opus"},
  ]
  assert select_audio(audios)["id"] == "a2"
