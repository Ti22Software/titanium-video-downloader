"""Disk-space preflight for multi-GB downloads.

Space model (documented, tunable): segments land in the workdir
(~1x estimate), the mux output lands beside the final file (~1x),
plus HEADROOM each. Same filesystem → ~2x + 2x headroom total;
split across filesystems (see --temp-dir) → each side checked alone.
Unknown free space (NAS/cloud drives that can't be stat'ed) warns and
proceeds — fail-open on unknown, fail-closed on known-short.
"""

import shutil

from .models import audio_bytes, video_bytes

HEADROOM_BYTES = 256 * 1024 * 1024


def free_bytes(path):
  """Free bytes on path's filesystem, or None when unreadable."""
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
  from ..extractors.base import fail
  import sys
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
