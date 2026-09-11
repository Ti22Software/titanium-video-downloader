"""Runtime path resolution (frozen-aware later; dev/CWD today).

Future residents: install_dir(), config_dir(), browsers_dir().
"""

import os
import shutil
from pathlib import Path

from ..extractors.base import fail


def user_path(raw):
  """User-supplied path with shell conventions applied.

  expanduser() (~) + expandvars() ($VAR) + Path, in one choke point for
  every path flag. No-op when the shell already expanded; lifesaver for
  dash, quoted strings, and GUI front-ends passing literals. None stays
  None (optional flags).
  """
  if raw is None:
    return None
  return Path(os.path.expandvars(os.path.expanduser(raw)))


def _bundled_ffmpeg():
  """imageio-ffmpeg static binary path, or None if the extra is absent."""
  try:
    import imageio_ffmpeg
  except ImportError:
    return None
  try:
    exe = Path(imageio_ffmpeg.get_ffmpeg_exe()).expanduser()
  except Exception:
    return None
  if exe.is_file() and os.access(exe, os.X_OK):
    return str(exe)
  return None


def ffmpeg_path(explicit=None):
  """Resolve the ffmpeg binary.

  Order: --ffmpeg-path > imageio-ffmpeg extra > system PATH.
  The extra is present by default (all our envs/CI/bundles install it);
  minimal installs without it fall back to system ffmpeg. Never returns
  a non-executable path — fails naming the source instead.
  """
  if explicit:
    exe = Path(explicit).expanduser()
    if exe.is_file() and os.access(exe, os.X_OK):
      return str(exe)
    fail(f"--ffmpeg-path not usable (missing or not executable): {explicit}")
  bundled = _bundled_ffmpeg()
  if bundled:
    return bundled
  found = shutil.which("ffmpeg")
  if found:
    return found
  fail("no ffmpeg found (tried --ffmpeg-path, imageio-ffmpeg extra, PATH).")
