#!/usr/bin/env python3
"""studygateway-dl: single-file streaming video downloader for studygateway.com.

Pipeline (downstream-first, no login yet):
  embed URL (embed.vhx.tv/videos/...) -> window.OTTData.config_url
  config URL (player.vimeo.com/video/.../config?...) -> request.files.dash playlist URL
  playlist.json -> video rendition + AAC audio rendition segment URLs
  segments -> video.mp4 + audio.m4a -> ffmpeg -c copy mux -> out.mp4

Usage:
  sg_dl.py EMBED_OR_CONFIG_URL [OUTPUT] [--list-qualities] [--quality Q]
                                [--output PATH] [--concurrency N] [--keep-intermediate]
"""

import argparse
import base64
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

try:
  from tqdm import tqdm
except ImportError:
  tqdm = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
EMBED_ORIGIN_HEADERS = {
  "User-Agent": UA,
  "Origin": "https://embed.vhx.tv",
  "Referer": "https://embed.vhx.tv/",
  "Accept": "*/*",
  "Accept-Language": "en-US,en;q=0.5",
}
EMBED_DOC_HEADERS = {
  "User-Agent": UA,
  "Referer": "https://watch.studygateway.com/",
  "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
             "image/avif,image/webp,image/apng,*/*;q=0.8"),
  "Accept-Language": "en-US,en;q=0.5",
  "Upgrade-Insecure-Requests": "1",
}

TIMEOUT = 30
RETRIES = 5


def fail(msg, code=1):
  print(f"error: {msg}", file=sys.stderr)
  sys.exit(code)


def fetch_json(session, url, what):
  try:
    r = session.get(url, headers=EMBED_ORIGIN_HEADERS, timeout=TIMEOUT)
  except requests.RequestException as e:
    fail(f"could not fetch {what}: {e}\n(URLs expire fast — re-paste a fresh URL and retry.)")
  if r.status_code in (401, 403):
    fail(f"{what} rejected (HTTP {r.status_code}) — URL expired. "
         "Re-paste a fresh embed/config URL and retry.")
  if r.status_code != 200:
    fail(f"{what} returned HTTP {r.status_code}.")
  try:
    return r.json()
  except ValueError:
    fail(f"{what} did not return JSON.")


def extract_ottdata(embed_html):
  """Return (config_url, title, video_id) from embed.vhx.tv document HTML."""
  marker = "window.OTTData"
  i = embed_html.find(marker)
  if i < 0:
    return None, None, None
  eq = embed_html.find("=", i)
  end = embed_html.find("</script>", eq)
  if eq < 0 or end < 0:
    return None, None, None
  blob = embed_html[eq + 1:end].strip().rstrip(";")
  try:
    data = json.loads(blob)
  except ValueError:
    return None, None, None
  video = data.get("video") or {}
  return data.get("config_url"), video.get("title"), video.get("id")


def pick_playlist_url(config):
  """Return (primary_url, fallback_url, quality_map) from player config JSON."""
  try:
    dash = config["request"]["files"]["dash"]
    cdns = dash["cdns"]
    default = dash.get("default_cdn")
  except (KeyError, TypeError):
    fail("config JSON has no request.files.dash — unexpected player response.")
  if default not in cdns:
    default = next(iter(cdns))
  ordered = [default] + [k for k in cdns if k != default]
  urls = []
  for k in ordered:
    cdn = cdns[k] or {}
    urls.append(cdn.get("avc_url") or cdn.get("url"))
  primary = urls[0] if len(urls) > 0 else None
  fallback = urls[1] if len(urls) > 1 else None
  if not primary:
    fail("config JSON has no dash CDN URL.")
  qmap = {}
  for s in dash.get("streams_avc") or dash.get("streams") or []:
    if s.get("id") and s.get("quality"):
      qmap[s["id"]] = s["quality"]
  return primary, fallback, qmap


def parse_playlist(pl):
  videos = []
  for v in pl.get("video") or []:
    if not (v.get("segments") and v.get("init_segment")):
      continue
    videos.append({
      "id": v.get("id"),
      "width": v.get("width"),
      "height": v.get("height"),
      "bitrate": v.get("bitrate") or 0,
      "codecs": v.get("codecs"),
      "duration": v.get("duration"),
      "init_segment": v.get("init_segment"),
      "base_url": v.get("base_url") or pl.get("base_url") or "",
      "segments": v.get("segments"),
    })
  audios = []
  for a in pl.get("audio") or []:
    if not (a.get("segments") and a.get("init_segment")):
      continue
    audios.append({
      "id": a.get("id"),
      "bitrate": a.get("bitrate") or 0,
      "codecs": a.get("codecs"),
      "mime_type": a.get("mime_type"),
      "init_segment": a.get("init_segment"),
      "base_url": a.get("base_url") or pl.get("base_url") or "",
      "segments": a.get("segments"),
    })
  if not videos:
    fail("playlist.json has no video renditions.")
  if not audios:
    fail("playlist.json has no audio renditions.")
  videos.sort(key=lambda v: v["bitrate"], reverse=True)
  return videos, audios


def label_for(v):
  h = v.get("height")
  return f"{h}p" if h else v.get("id", "?")[:8]


def select_video(videos, quality):
  if quality in ("best", "highest", "max"):
    return videos[0]
  m = re.fullmatch(r"(\d+)\s*p?", quality.strip().lower())
  if m:
    want = int(m.group(1))
    for v in videos:
      if v.get("height") == want:
        return v
    avail = ", ".join(label_for(v) for v in videos)
    fail(f"quality '{quality}' not available (have: {avail}).")
  # fall back to exact id prefix match
  for v in videos:
    if v.get("id", "").startswith(quality):
      return v
  avail = ", ".join(label_for(v) for v in videos)
  fail(f"unknown quality '{quality}' (have: {avail}, best).")


def select_audio(audios):
  aac = [a for a in audios if (a.get("codecs") or "").startswith("mp4a.")]
  pool = aac or audios
  pool = sorted(pool, key=lambda a: a["bitrate"], reverse=True)
  return pool[0]


def resolve_segments(playlist_url, rendition):
  base = urljoin(playlist_url, rendition["base_url"])
  return [urljoin(base, s["url"]) for s in rendition["segments"]]


def sanitize(name):
  name = re.sub(r"[^\w\s.\-()]", "_", name).strip()
  name = re.sub(r"\s+", " ", name)
  return name or "video"


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
  bar = None
  if tqdm and todo:
    bar = tqdm(total=len(seg_urls), initial=len(seg_urls) - len(todo),
               unit="seg", desc=kind)
  elif todo:
    print(f"{kind}: {len(seg_urls) - len(todo)}/{len(seg_urls)} segments cached",
          file=sys.stderr)

  errors = []
  completed = []

  def _mark(idx):
    done.add(idx)
    completed.append(idx)
    if bar:
      bar.update(1)
    elif len(completed) % 25 == 0 or len(done) == len(seg_urls):
      print(f"{kind}: {len(done)}/{len(seg_urls)} segments", file=sys.stderr)

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
  if len(done) != len(seg_urls):
    fail(f"{kind} incomplete: {len(done)}/{len(seg_urls)} segments.")

  out = workdir / f"{kind}.mp4"
  with open(out, "wb") as f:
    f.write(base64.b64decode(rendition["init_segment"]))
    for i in range(len(seg_urls)):
      with open(parts / f"{i:05d}.m4s", "rb") as sf:
        shutil.copyfileobj(sf, f)
  return out


def mux(video_path, audio_path, out_path):
  ff = shutil.which("ffmpeg")
  if not ff:
    fail("ffmpeg not found in PATH.")
  cmd = [ff, "-y", "-i", str(video_path), "-i", str(audio_path),
         "-c", "copy", "-movflags", "+faststart", str(out_path)]
  r = subprocess.run(cmd, capture_output=True, text=True)
  if r.returncode != 0:
    print(r.stderr[-3000:], file=sys.stderr)
    fail("ffmpeg mux failed.")
  return out_path


def fetch_text(session, url):
  try:
    r = session.get(url, headers=EMBED_DOC_HEADERS, timeout=TIMEOUT)
  except requests.RequestException as e:
    fail(f"could not fetch embed page: {e}")
  if r.status_code in (401, 403):
    fail("embed page rejected — auth-user-token expired. Re-paste a fresh iframe URL.")
  if r.status_code != 200:
    fail(f"embed page returned HTTP {r.status_code}.")
  return r.text


def resolve_config_url(session, input_url):
  if "player.vimeo.com" in input_url and "/config" in input_url:
    return input_url, None, None
  if "embed.vhx.tv" not in input_url:
    fail("input must be an embed.vhx.tv URL or a player.vimeo.com config URL.")
  html = fetch_text(session, input_url)
  config_url, title, vid = extract_ottdata(html)
  if not config_url:
    fail("could not find window.OTTData.config_url in embed page "
         "(token expired or page format changed).")
  return config_url, title, vid


def main(argv=None):
  ap = argparse.ArgumentParser(
    description="Download a single studygateway video to an MP4 file.")
  ap.add_argument("input_url", help="embed.vhx.tv iframe URL or player.vimeo.com config URL")
  ap.add_argument("output_pos", nargs="?", default=None,
                  help="output MP4 path (shorthand for --output)")
  ap.add_argument("--list-qualities", action="store_true",
                  help="list available renditions and exit")
  ap.add_argument("--quality", default="best",
                  help="height label (1080p/720p/540p/360p/240p), 'best', or rendition id prefix")
  ap.add_argument("--output", "-o", default=None, help="output MP4 path")
  ap.add_argument("--concurrency", "-j", type=int, default=8,
                  help="parallel segment downloads (default 8)")
  ap.add_argument("--keep-intermediate", action="store_true",
                  help="keep video/audio intermediates and parts dir")
  args = ap.parse_args(argv)
  if args.output and args.output_pos and args.output != args.output_pos:
    fail("pass the output path either positionally or via --output, not both.")
  output_arg = args.output or args.output_pos

  session = requests.Session()
  session.headers.update({"User-Agent": UA})

  config_url, title, _vid = resolve_config_url(session, args.input_url)
  config = fetch_json(session, config_url, "player config")
  playlist_url, fallback_url, qmap = pick_playlist_url(config)

  playlist = None
  for url in [u for u in (playlist_url, fallback_url) if u]:
    try:
      r = session.get(url, headers=EMBED_ORIGIN_HEADERS, timeout=TIMEOUT)
      if r.status_code in (401, 403):
        continue
      r.raise_for_status()
      playlist = r.json()
      playlist_url = url
      break
    except (requests.RequestException, ValueError):
      continue
  if playlist is None:
    fail("playlist.json fetch failed on all CDNs — URLs expired. "
         "Re-paste a fresh URL (embed page first, config within 60s).")

  videos, audios = parse_playlist(playlist)

  if args.list_qualities:
    print(f"{'quality':<8}{'size':>10}  {'bitrate':>9}  id")
    for v in sorted(videos, key=lambda v: v.get("height") or 0, reverse=True):
      total = sum(s.get("size", 0) for s in v["segments"])
      q = qmap.get(v["id"], label_for(v))
      print(f"{q:<8}{total / 1e6:>9.1f}M  {v['bitrate']:>9}  {v['id']}")
    return 0

  video = select_video(videos, args.quality)
  audio = select_audio(audios)
  v_urls = resolve_segments(playlist_url, video)
  a_urls = resolve_segments(playlist_url, audio)

  name = sanitize(title or config.get("video", {}).get("title") or "video")
  q = qmap.get(video["id"], label_for(video))
  out_path = Path(output_arg) if output_arg else Path(f"{name} [{q}].mp4")
  if out_path.exists():
    fail(f"output exists: {out_path} (remove it or pass a different output path).")
  if out_path.suffix.lower() != ".mp4":
    out_path = out_path.with_suffix(".mp4")

  workdir = out_path.parent / (out_path.stem + ".sgdl")
  workdir.mkdir(parents=True, exist_ok=True)
  print(f"video: {q} {video['width']}x{video['height']} "
        f"({len(v_urls)} segs), audio: {audio['codecs']} ({len(a_urls)} segs)",
        file=sys.stderr)
  v_path = download_rendition("video", video, v_urls, workdir, session, args.concurrency)
  a_path = download_rendition("audio", audio, a_urls, workdir, session, args.concurrency)
  mux(v_path, a_path, out_path)
  if not args.keep_intermediate:
    shutil.rmtree(workdir, ignore_errors=True)
  print(str(out_path))
  return 0


if __name__ == "__main__":
  sys.exit(main())
