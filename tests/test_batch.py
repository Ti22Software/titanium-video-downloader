"""Offline tests: batch file parsing, dedupe, run_batch ledger."""

import argparse

import pytest

from titanium_video_downloader.core.batch import dedupe_entries, is_url, parse_batch_file


def test_is_url_heuristic():
  assert is_url("https://vimeo.com/1")
  assert is_url("http://x.y/z")
  assert is_url("HTTPS://VIMEO.COM/1")
  assert not is_url("out.mp4")
  assert not is_url("")
  assert not is_url(None)


def test_parse_batch_file(tmp_path):
  f = tmp_path / "b.txt"
  f.write_text("# comment\n\nhttps://a/1 720p\nhttps://a/2\n  # indented\nhttps://a/3   best  \n")
  assert parse_batch_file(str(f)) == [
    ("https://a/1", "720p"), ("https://a/2", None), ("https://a/3", "best")]


def test_parse_batch_file_missing_and_empty(tmp_path):
  with pytest.raises(SystemExit):
    parse_batch_file(str(tmp_path / "nope.txt"))
  empty = tmp_path / "e.txt"
  empty.write_text("# nothing here\n\n")
  with pytest.raises(SystemExit):
    parse_batch_file(str(empty))


def test_dedupe_exact_and_quality_split():
  entries = [("https://a/1", None), ("https://a/1", None),
             ("https://a/1", "720p"), ("https://a/1", "best"),
             ("https://a/2", None)]
  unique, notes = dedupe_entries(entries)
  assert unique == [("https://a/1", None), ("https://a/1", "720p"), ("https://a/2", None)]
  assert len(notes) == 2
  assert all("duplicate" in n for n in notes)


def _batch_args(**kw):
  base = dict(no_auth=False, cookies=None, use_browser_login=False, headed=False,
              keep_intermediate=False, no_space_check=True, login_only=False,
              pick=False, quality="best", on_missing_quality="fallback",
              debug_login=False, output=None, output_dir=None, temp_dir=None)
  base.update(kw)
  return argparse.Namespace(**base)


def test_run_batch_ledger_and_exit_code(monkeypatch, tmp_path):
  from titanium_video_downloader import cli as cli_mod

  calls = []

  def _fake_resolve(session, args, url, quality, tag, batch, authed,
                      output_arg=None, output_dir=None, temp_dir=None):
    calls.append(("resolve", url))
    if "bad" in url:
      raise SystemExit(1)
    if "skip" in url:
      from titanium_video_downloader.core.batch import EntrySkip
      raise EntrySkip("output exists")
    return {"action": "download", "url": url, "video": {"id": "v"},
            "audio": {"id": "a"}, "est": 10,
            "workdir": tmp_path / "w", "outdir": tmp_path}

  def _fake_download(session, args, bundle, tag, batch):
    calls.append(("download", bundle["url"]))
    return "ok"

  monkeypatch.setattr(cli_mod, "_resolve_entry", _fake_resolve)
  monkeypatch.setattr(cli_mod, "_download_entry", _fake_download)
  args = _batch_args()
  entries = [("https://a/ok1", None), ("https://a/skip", None), ("https://a/bad", None),
             ("https://a/ok2", None)]
  assert cli_mod.run_batch(args, entries, object()) == 1
  assert [c for c in calls if c[0] == "resolve"] == [
    ("resolve", u) for u, _ in entries]
  assert [c for c in calls if c[0] == "download"] == [
    ("download", "https://a/ok1"), ("download", "https://a/ok2")]


def test_run_batch_upfront_gate_sums(tmp_path, monkeypatch):
  from titanium_video_downloader import cli as cli_mod

  work = tmp_path / "work"
  work.mkdir()
  outd = tmp_path / "out"
  outd.mkdir()
  gated = {}

  def _fake_resolve(session, args, url, quality, tag, batch, authed,
                      output_arg=None, output_dir=None, temp_dir=None):
    return {"action": "download", "url": url,
            "video": {"segments": [{"size": 100}]},
            "audio": {"segments": [{"size": 50}]},
            "workdir": work, "outdir": outd}

  def _fake_download(session, args, bundle, tag, batch):
    return "ok"

  def _fake_check(path, needed, label):
    gated[label] = needed

  monkeypatch.setattr(cli_mod, "_resolve_entry", _fake_resolve)
  monkeypatch.setattr(cli_mod, "_download_entry", _fake_download)
  monkeypatch.setattr(cli_mod, "check_space", _fake_check)
  args = _batch_args(no_space_check=False)
  assert cli_mod.run_batch(args, [("https://a/1", None), ("https://a/2", None)],
                           object()) == 0
  # 2 videos x (100+50) estimates + headroom each side, no keep → max work
  from titanium_video_downloader.core.disk import HEADROOM_BYTES
  assert gated["batch temporary files"] == 150 + HEADROOM_BYTES
  assert gated["batch outputs"] == 300 + HEADROOM_BYTES
