"""Disk-space preflight for multi-GB downloads.

Space model (documented, tunable): segments land in the workdir
(~1x estimate), the mux output lands beside the final file (~1x),
plus HEADROOM each. Same filesystem → ~2x + headroom total;
split across filesystems (see --temp-dir) → each side checked alone.
Unknown free space (NAS/cloud drives that can't be stat'ed) warns and
proceeds — fail-open on unknown, fail-closed on known-short.
"""

import errno
import os
import shutil
import sys

from .models import audio_bytes, video_bytes
from ..extractors.base import fail

HEADROOM_BYTES = 256 * 1024 * 1024


def free_bytes(path):
  """Usable free bytes on path's filesystem, or None when unreadable.

  Unix uses statvfs f_bavail (excludes root-reserved blocks — matches df
  "Available"); os.statvfs doesn't exist on Windows, which (plus any
  OSError) falls back to shutil.disk_usage. Unreadable mounts → None.
  """
  try:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize
  except (AttributeError, OSError):
    pass
  try:
    return shutil.disk_usage(path).free
  except OSError:
    return None


def human_bytes(n):
  """Decimal human form matching the size column (MB = 1e6)."""
  n = float(n or 0)
  for unit, factor in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
    if n >= factor:
      return f"{n / factor:.1f} {unit}"
  return f"{int(n)} B"


def download_estimate(video, audio):
  """Estimated total download bytes (single source of truth).

  Shared with quality_items so listings and preflight can't drift.
  """
  return video_bytes(video) + audio_bytes(audio)


def check_space(path, needed, label):
  """Fail fast when known-short; warn-and-proceed when unreadable."""
  free = free_bytes(path)
  if free is None:
    print(f"note: could not read free space for {label} '{path}' "
          "(network/cloud drive?) — proceeding without a space check",
          file=sys.stderr)
    return
  if free < needed:
    fail(f"insufficient disk space for {label} '{path}': need "
         f"~{human_bytes(needed)}, have {human_bytes(free)} free "
         "(use --no-space-check to override, or a smaller --quality).")


def io_fail(operation, exc):
  """Clean failure for local write errors (GUI-bubblable: single stderr
  line via fail(), exit 1 — the future error event). Partial state on
  disk stays resume-compatible: manifests list only fully-written
  segments, so re-running resumes instead of restarting."""
  err = exc.errno if isinstance(exc, OSError) else None
  where = f" ({exc.filename})" if isinstance(exc, OSError) and exc.filename else ""
  if err == errno.ENOSPC:
    fail(f"disk full during {operation}{where} — free space or point "
         "--temp-dir/--output-dir elsewhere and re-run to resume.")
  if err in (errno.EACCES, errno.EPERM, errno.EROFS):
    fail(f"cannot write during {operation}{where}: {exc.strerror or exc} — "
         "check directory permissions.")
  detail = exc.strerror or exc
  fail(f"write failed during {operation}{where}: {detail}.")
