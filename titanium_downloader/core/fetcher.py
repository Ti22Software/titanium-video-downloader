"""Network fetch + concurrent segment download with resume manifest."""

import base64
import concurrent.futures
import json
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

from .session import EMBED_DOC_HEADERS, EMBED_ORIGIN_HEADERS, RETRIES, TIMEOUT
from ..extractors.base import fail


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


def _get_with_retry(session, url, idx):
  last = None
  for attempt in range(RETRIES):
    try:
      r = session.get(url, headers=EMBED_ORIGIN_HEADERS, timeout=TIMEOUT)
      if r.status_code in (401, 403):
        raise requests.HTTPError(f"HTTP {r.status_code} (URL expired?)")
      r.raise_for_status()
      return idx, r.content
    except Exception as e:
      last = e
      time.sleep(min(2 ** attempt, 8))
  raise RuntimeError(f"segment {idx} failed after {RETRIES} tries: {last}")


def download_rendition(kind, rendition, seg_urls, workdir, session, concurrency):
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
          if i not in done or not (parts / f"{i:05d}.m4s").exists()]
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

  def _mark(idx):
    with lock:
      done.add(idx)
      if bar:
        bar.update(1)
      else:
        _render()

  if todo:
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
      futs = {ex.submit(_get_with_retry, session, seg_urls[i], i): i for i in todo}
      try:
        for fut in concurrent.futures.as_completed(futs):
          try:
            idx, data = fut.result()
          except RuntimeError as e:
            errors.append(str(e))
          else:
            (parts / f"{idx:05d}.m4s").write_bytes(data)
            _mark(idx)
      finally:
        if bar:
          bar.close()
      manifest.write_text(json.dumps(sorted(done)))
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
  with open(out, "wb") as f:
    f.write(base64.b64decode(rendition["init_segment"]))
    for i in range(len(seg_urls)):
      with open(parts / f"{i:05d}.m4s", "rb") as sf:
        shutil.copyfileobj(sf, f)
  return out
