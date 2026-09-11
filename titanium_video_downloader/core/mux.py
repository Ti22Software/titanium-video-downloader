"""ffmpeg mux with progress parsing + binary resolution."""

import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..extractors.base import fail
from .disk import io_fail
from .paths import ffmpeg_path


def mux(video_path, audio_path, out_path, total_duration=None, ffmpeg=None):
  ff = ffmpeg_path(ffmpeg)
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

  # stdin=DEVNULL: ffmpeg must never hold the user's tty. It enables
  # interactive mode on tty stdin and restores termios only on clean exit;
  # a crash (e.g. segfault) would otherwise leave echo disabled, hiding
  # all typed input until `reset`. Progress already comes via pipe:1.
  proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                          text=True)
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
    _ffmpeg_error("mux", err)
  el = time.monotonic() - start
  _write_mux_line(f"Muxing: done in {el:.1f}s")
  sys.stderr.write("\n")
  sys.stderr.flush()
  return out_path


def _ffmpeg_error(what, err_text):
  """Classify an ffmpeg failure into one GUI-bubblable error line.

  A starved disk usually surfaces as write/trailer/closing EIO text rather
  than a clean ENOSPC string, so match those markers first after the
  explicit case. Anything unrecognized keeps the legacy generic message
  plus the raw tail for unfamiliar failures.
  """
  err = err_text or ""
  low = err.lower()
  if "no space left" in low:
    fail(f"disk full during ffmpeg {what} — free space or point "
         "--temp-dir/--output-dir elsewhere and re-run to resume "
         "(segments are kept).")
  if ("error writing trailer" in low or "error closing file" in low
          or ("input/output error" in low and "muxing" in low)):
    print(err[-3000:], file=sys.stderr)
    fail(f"ffmpeg {what} hit a disk I/O error (often a full disk — "
         "check free space, then re-run to resume).")
  print(err[-3000:], file=sys.stderr)
  fail(f"ffmpeg {what} failed.")


def remux_concat(parts_dir, out_path, ffmpeg=None):
  """Remux muxed-TS parts (e.g. HLS) into MP4 via concat demuxer, -c copy.

  Tries the resolved binary first, then falls back to system ffmpeg:
  imageio 7.0.2-static segfaults in its mpegts demuxer on some files
  (verified: Rumble TS), while system 6.1.1 handles them. Fail only if
  every candidate fails.
  """
  ff = ffmpeg_path(ffmpeg)
  parts_dir = Path(parts_dir)
  segs = sorted(parts_dir.glob("*.ts"))
  if not segs:
    fail(f"no .ts segments in {parts_dir}.")
  out_path = Path(out_path)
  print(f"\nRemuxing {len(segs)} TS segments -> {out_path.resolve()}", file=sys.stderr)
  lst = parts_dir / "concat.txt"
  try:
    with open(lst, "w") as f:
      for s in segs:
        # Absolute entries: the concat demuxer resolves relative ones
        # against the list file's own directory, doubling relative paths.
        f.write(f"file '{s.resolve().as_posix()}'\n")
  except OSError as e:
    io_fail("remux list write", e)
  candidates = [ff]
  sys_ff = shutil.which("ffmpeg")
  if sys_ff and sys_ff != ff:
    candidates.append(sys_ff)
  last_err = ""
  for i, cand in enumerate(candidates):
    cmd = [cand, "-y", "-v", "error", "-nostats",
           "-f", "concat", "-safe", "0", "-i", str(lst),
           "-c", "copy", "-movflags", "+faststart", str(out_path)]
    # stdin=DEVNULL: see mux() above — a crashing ffmpeg must not keep
    # the tty (same echo-off terminal breakage, same one-line cure).
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, text=True)
    if proc.returncode == 0:
      return out_path
    last_err = (proc.stderr or "")[-3000:]
    if i < len(candidates) - 1:
      print(f"note: ffmpeg {cand} failed (rc={proc.returncode}), "
            "retrying with system ffmpeg", file=sys.stderr)
  _ffmpeg_error("remux", last_err)
