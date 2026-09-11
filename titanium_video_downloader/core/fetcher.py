"""Network fetch + concurrent segment download with resume manifest."""

import base64
import concurrent.futures
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import requests

try:
  from tqdm import tqdm
except ImportError:
  tqdm = None

from .session import EMBED_DOC_HEADERS, EMBED_ORIGIN_HEADERS, RETRIES, TIMEOUT, UA
from ..extractors.base import fail
from .disk import io_fail


def fetch_json(session, url, what):
  try:
    r = session.get(url, headers=EMBED_ORIGIN_HEADERS, timeout=TIMEOUT)
  except requests.RequestException as e:
    fail(f"could not fetch {what}: {e}\n(URLs expire fast — re-paste a fresh URL and retry.)")
  if r.status_code == 410:
    fail(f"{what} token expired.")
  if r.status_code in (401, 403):
    fail(f"{what} rejected (HTTP {r.status_code}) — URL expired. "
         "Re-paste a fresh embed/config URL and retry.")
  if r.status_code != 200:
    fail(f"{what} returned HTTP {r.status_code}.")
  try:
    return r.json()
  except ValueError:
    fail(f"{what} did not return JSON.")


def fetch_text(session, url):
  try:
    r = session.get(url, headers=EMBED_DOC_HEADERS, timeout=TIMEOUT)
  except requests.RequestException as e:
    fail(f"could not fetch embed page: {e}")
  if r.status_code in (401, 403):
    fail("embed page rejected — session/token expired. Re-run to mint a fresh "
         "embed URL via login (tokens are hours-lived, not for pasting).")
  if r.status_code != 200:
    fail(f"embed page returned HTTP {r.status_code}.")
  return r.text


def _get_with_retry(session, url, idx, headers=None):
  last = None
  for attempt in range(RETRIES):
    try:
      r = session.get(url, headers=headers or EMBED_ORIGIN_HEADERS, timeout=TIMEOUT)
      if r.status_code in (401, 403):
        raise requests.HTTPError(f"HTTP {r.status_code} (URL expired?)")
      r.raise_for_status()
      return idx, r.content
    except Exception as e:
      last = e
      time.sleep(min(2 ** attempt, 8))
  raise RuntimeError(f"segment {idx} failed after {RETRIES} tries: {last}")


def fetch_hls_chunklist(session, tar_url, referer):
  """GET a tar-wrapped HLS chunklist.m3u8, return absolute segment URLs.

  Rumble serves variant playlists as tar members selected by the
  ``r_file``/``r_range`` query: download the slice, unpack with stdlib
  tarfile, parse the ``#EXTINF`` URIs inside. stdlib only, no m3u8 dep.
  """
  import io
  import tarfile
  from urllib.parse import urljoin
  headers = {
    "User-Agent": UA,
    "Referer": referer,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.5",
  }
  try:
    r = session.get(tar_url, headers=headers, timeout=TIMEOUT)
  except requests.RequestException as e:
    fail(f"could not fetch HLS chunklist: {e}")
  if r.status_code != 200:
    fail(f"HLS chunklist returned HTTP {r.status_code}.")
  data = r.content
  if data.lstrip().startswith(b"#EXTM3U"):
    # Served straight (r_file mechanism transmuxes server-side).
    text = data.decode("utf-8", "replace")
  else:
    try:
      tf = tarfile.open(fileobj=io.BytesIO(data))
      names = [n for n in tf.getnames() if n.endswith(".m3u8")]
      if not names:
        fail("HLS chunklist tar has no .m3u8 member.")
      text = tf.extractfile(names[0]).read().decode("utf-8", "replace")
    except (tarfile.TarError, KeyError, ValueError):
      fail("could not unpack HLS chunklist tar.")
  uris = [ln.strip() for ln in text.splitlines()
          if ln.strip() and not ln.strip().startswith("#")]
  if not uris:
    fail("HLS chunklist has no segments.")
  return [urljoin(tar_url, u) for u in uris]


# Manifest checkpoint cadence: flush sorted(done) this often, so a crash
# or ENOSPC mid-batch still leaves recent progress for resume.
MANIFEST_EVERY = 10


def download_rendition(kind, rendition, seg_urls, workdir, session, concurrency,
                       suffix=".m4s", headers=None, assemble=True):
  """Download segments with resume manifest; assemble the combined file.

  assemble=False skips writing kind.mp4 and returns the parts dir instead —
  for muxed single-streams (HLS-TS) whose remux reads parts directly.
  """
  parts = workdir / kind
  parts.mkdir(parents=True, exist_ok=True)
  manifest = workdir / f"{kind}.manifest.json"
  done = set()
  if manifest.exists():
    try:
      done = set(json.loads(manifest.read_text()))
    except ValueError:
      done = set()
  todo = [i for i in range(len(seg_urls))
          if i not in done or not (parts / f"{i:05d}{suffix}").exists()]
  total = len(seg_urls)
  bar = None
  if tqdm and todo:
    bar = tqdm(total=total, initial=total - len(todo),
               unit="seg", desc=kind)
  elif todo and done:
    print(f"{kind}: {len(done)}/{total} segments cached", file=sys.stderr)
  elif not todo:
    print(f"{kind}: {total}/{total} segments cached", file=sys.stderr)

  errors = []
  lock = threading.Lock()
  start = time.monotonic()
  last_print = [0.0]
  max_width = [0]

  def _write_line(text):
    max_width[0] = max(max_width[0], len(text))
    sys.stderr.write("\r" + text.ljust(max_width[0]))
    sys.stderr.flush()

  def _render(force=False):
    now = time.monotonic()
    if not force and now - last_print[0] < 0.2:
      return
    last_print[0] = now
    n = len(done)
    el = now - start
    rate = n / el if el > 0 and n > 0 else 0.0
    eta = f"{(total - n) / rate:.0f}s" if rate > 0 and n < total else "--"
    _write_line(f"{kind}: {n}/{total} segs, {rate:.1f} seg/s, ETA {eta}")

  def _flush_manifest():
    try:
      manifest.write_text(json.dumps(sorted(done)))
    except OSError as e:
      io_fail(f"{kind} manifest write", e)

  def _mark(idx):
    with lock:
      done.add(idx)
      if len(done) % MANIFEST_EVERY == 0:
        _flush_manifest()
      if bar:
        bar.update(1)
      else:
        _render()

  if todo:
    try:
      with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {ex.submit(_get_with_retry, session, seg_urls[i], i,
                          headers): i for i in todo}
        try:
          for fut in concurrent.futures.as_completed(futs):
            try:
              idx, data = fut.result()
            except RuntimeError as e:
              errors.append(str(e))
            else:
              try:
                (parts / f"{idx:05d}{suffix}").write_bytes(data)
              except OSError as e:
                io_fail(f"{kind} segment {idx} write", e)
              _mark(idx)
        finally:
          if bar:
            bar.close()
    except BaseException:
      _flush_manifest()
      raise
    _flush_manifest()
  if errors:
    fail(f"{kind} download had {len(errors)} failed segment(s), e.g.: {errors[0]}")
  if len(done) != total:
    fail(f"{kind} incomplete: {len(done)}/{total} segments.")
  if not bar:
    el = time.monotonic() - start
    _write_line(f"{kind}: {total}/{total} segs, done in {el:.1f}s")
    sys.stderr.write("\n")
    sys.stderr.flush()

  out = workdir / f"{kind}.mp4"
  if not assemble:
    return parts
  if _assembly_fresh(out, parts, suffix, len(seg_urls), rendition.get("init_segment")):
    print(f"{kind}: combined file fresh, skipping assembly", file=sys.stderr)
    return out
  try:
    with open(out, "wb") as f:
      if rendition.get("init_segment"):
        f.write(base64.b64decode(rendition["init_segment"]))
      for i in range(len(seg_urls)):
        with open(parts / f"{i:05d}{suffix}", "rb") as sf:
          shutil.copyfileobj(sf, f)
  except OSError as e:
    io_fail(f"{kind} assembly write", e)
  return out


def _assembly_fresh(out, parts, suffix, count, init_segment):
  """True when the combined file already matches current parts.

  All three must hold: exists, mtime newer than the newest part, and byte
  size equal to init + parts sum (stat-only, milliseconds). A kill
  mid-assembly leaves a short size or stale mtime and re-assembles.
  """
  try:
    out_stat = out.stat()
  except OSError:
    return False
  try:
    newest = 0.0
    total = len(base64.b64decode(init_segment)) if init_segment else 0
    for i in range(count):
      st = (parts / f"{i:05d}{suffix}").stat()
      if st.st_mtime > newest:
        newest = st.st_mtime
      total += st.st_size
  except OSError:
    return False
  return out_stat.st_mtime >= newest and out_stat.st_size == total
