"""Offline tests: rendition rows + pick index for list/pick modes."""

import pytest

from titanium_downloader.core.models import pick_index, quality_items


def test_quality_items_shape_and_order():
  videos = [
    {"id": "v360", "width": 640, "height": 360, "bitrate": 800,
     "codecs": "avc1", "segments": [{"size": 5}, {"size": 7}]},
    {"id": "v1080", "width": 1920, "height": 1080, "bitrate": 5000,
     "codecs": "avc1", "segments": [{"size": 100}]},
  ]
  items = quality_items(videos, {"v1080": "1080p"})
  assert [i["id"] for i in items] == ["v1080", "v360"]
  assert items[0]["quality"] == "1080p"
  assert items[0]["size"] == 100
  assert items[1] == {"id": "v360", "quality": "360p", "width": 640,
                      "height": 360, "bitrate": 800, "size": 12, "codecs": "avc1"}


def test_pick_index_default_and_bounds():
  assert pick_index(3, "") == 1
  assert pick_index(3, " 2 ") == 2
  with pytest.raises(SystemExit):
    pick_index(3, "0")
  with pytest.raises(SystemExit):
    pick_index(3, "4")
  with pytest.raises(SystemExit):
    pick_index(3, "x")
