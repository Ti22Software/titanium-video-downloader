"""ffmpeg mux with progress parsing + binary resolution."""

import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..extractors.base import fail


def mux(video_path, audio_path, out_path, total_duration=None):
  ff = shutil.which("ffmpeg")
  if not ff:
    fail("ffmpeg not found in PATH.")
  out_path = Path(out_path)
  print(f"\nMuxing -> {out_path.resolve()}", file=sys.stderr)
  cmd = [ff, "-y", "-v", "error", "-nostats",
         "-i", str(video_path), "-i", str(audio_path),
         "-c", "copy", "-movflags", "+faststart",
         "-progress", "pipe:1", str(out_path)]
  total_us = int(total_duration * 1_000_000) if total_duration else None
  start = time.monotonic()
  last_print = [0.0]
  max_width = [0]

  def _write_mux_line(text):
    max_width[0] = max(max_width[0], len(text))
    sys.stderr.write("\r" + text.ljust(max_width[0]))
    sys.stderr.flush()

  proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)
  try:
    for line in proc.stdout:
      line = line.strip()
      if not line.startswith("out_time_ms="):
        continue
      try:
        cur = int(line.split("=", 1)[1])
      except ValueError:
        continue
      if cur <= 0:
        continue
      now = time.monotonic()
      if now - last_print[0] < 0.2:
        continue
      last_print[0] = now
      el = now - start
      if total_us:
        frac = min(cur / total_us, 1.0)
        eta = el * (1 - frac) / frac if frac > 0 else 0.0
        _write_mux_line(f"Muxing: {frac * 100:.0f}%, ETA {eta:.0f}s")
      else:
        _write_mux_line(f"Muxing: {el:.0f}s elapsed...")
  finally:
    if proc.stdout:
      proc.stdout.close()
  rc = proc.wait()
  err = proc.stderr.read() if proc.stderr else ""
  if proc.stderr:
    proc.stderr.close()
  if rc != 0:
    print(err[-3000:], file=sys.stderr)
    fail("ffmpeg mux failed.")
  el = time.monotonic() - start
  _write_mux_line(f"Muxing: done in {el:.1f}s")
  sys.stderr.write("\n")
  sys.stderr.flush()
  return out_path
