"""Offline tests: user paths, -o/-output-dir harmony, resolved notes."""

from pathlib import Path

from titanium_video_downloader.core.paths import user_path


def test_user_path_expands(monkeypatch):
  monkeypatch.setenv("TI22_DIRS_TEST_HOME", "/tmp/xyz")
  assert user_path("~/videos") == Path.home() / "videos"
  assert user_path("$TI22_DIRS_TEST_HOME/deep") == Path("/tmp/xyz/deep")
  assert user_path("/abs/path") == Path("/abs/path")
  assert user_path("relative.mp4") == Path("relative.mp4")
  assert user_path(None) is None


def test_bare_filename_joins_output_dir():
  from titanium_video_downloader import cli  # noqa: F401
  given = user_path("clip.mp4")
  assert given.parent == Path(".")
  joined = Path("/tmp/out") / given.name
  assert joined == Path("/tmp/out/clip.mp4")


def test_dirful_output_stands_alone():
  given = user_path("/tmp/a/b.mp4")
  assert given.parent != Path(".")
  given2 = user_path("rel/dir/c.mp4")
  assert given2.parent != Path(".")


def test_output_dir_help_documents_harmony(capsys):
  import pytest
  from titanium_video_downloader import cli as cli_mod
  with pytest.raises(SystemExit) as exc:
    cli_mod.main(["--help"])
  assert exc.value.code == 0
  out = capsys.readouterr().out
  assert "bare -o filenames" in out
  assert "dir-ful -o wins" in out


def test_help_loads_no_env_files(monkeypatch, tmp_path):
  """--help must exit during parse_args, before load_dotenv runs, so
  tests (and --help users) never absorb real .env files into os.environ."""
  import os
  import pytest
  from titanium_video_downloader import cli as cli_mod
  marker = "TI22_VIDEO_DL_TEST_HELP_GUARD"
  monkeypatch.delenv(marker, raising=False)
  monkeypatch.chdir(tmp_path)
  (tmp_path / ".env").write_text(f"{marker}=fromfile\n")
  with pytest.raises(SystemExit):
    cli_mod.main(["--help"])
  assert marker not in os.environ
