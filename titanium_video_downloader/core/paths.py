"""Runtime path resolution (frozen-aware later; dev/CWD today).

Future residents: install_dir(), config_dir(), browsers_dir().
"""

import os
import shutil
from pathlib import Path

from ..extractors.base import fail
from .envfile import default_config_dir


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


def config_relative_path(raw):
  """Resolve a user-supplied file path with config-dir fallback.

  Absolute paths (after ~/ $VAR expansion) pass through untouched.
  Relative names try CWD first, then the app config dir — mirroring
  find_dotenv's ./ → config-dir order. A miss in both places resolves
  to the config-dir join so load failures name a concrete tried path.
  """
  import sys
  p = user_path(raw)
  if p is None or p.is_absolute():
    return p
  if (Path.cwd() / p).is_file():
    return Path.cwd() / p
  target = default_config_dir() / p
  print(f"note: resolving relative path against config dir: {target}",
        file=sys.stderr)
  return target


def _is_executable(exe):
  """Executability check that works on POSIX and Windows.

  POSIX honors exec bits via os.access. Windows has none (readability ≈
  executability for access checks, and chmod() is advisory-only), so there
  a PATHEXT extension marks an executable file. Either way this is only a
  pre-check for a clear error message — the real proof is spawning it.
  """
  if os.name == "nt":
    pathext = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    wanted = {e.upper() for e in pathext.split(";") if e}
    return Path(exe).suffix.upper() in wanted
  return os.access(exe, os.X_OK)


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
  if exe.is_file() and _is_executable(exe):
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
    if exe.is_file() and _is_executable(exe):
      return str(exe)
    fail(f"--ffmpeg-path not usable (missing or not executable): {explicit}")
  bundled = _bundled_ffmpeg()
  if bundled:
    return bundled
  found = shutil.which("ffmpeg")
  if found:
    return found
  fail("no ffmpeg found (tried --ffmpeg-path, imageio-ffmpeg extra, PATH).")
