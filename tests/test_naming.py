"""Offline tests: portable naming (mirrors live-verified behavior)."""

from pathlib import Path

from titanium_video_downloader.core.naming import sanitize, sanitize_path


def test_sanitize_collapses_whitespace():
  assert sanitize("  Hello   World  ") == "Hello World"


def test_sanitize_reserved_chars():
  assert sanitize('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"


def test_sanitize_reserved_names():
  assert sanitize("CON") == "_CON"
  assert sanitize("com1") == "_com1"
  assert sanitize("normal") == "normal"


def test_sanitize_empty_falls_back():
  assert sanitize("...") == "video"
  assert sanitize("") == "video"


def test_sanitize_path_keeps_dir_and_suffix():
  assert sanitize_path("some/dir/a:b.mp4") == Path("some/dir/a_b.mp4")
