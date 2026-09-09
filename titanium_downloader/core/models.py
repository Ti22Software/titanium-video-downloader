"""Rendition shapes: playlist parse, quality selection, segment resolve."""

import re
from urllib.parse import urljoin

from ..extractors.base import fail


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
      "framerate": v.get("framerate"),
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
      "sample_rate": a.get("sample_rate"),
      "channels": a.get("channels"),
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


def audio_label(codecs):
  c = (codecs or "").lower()
  if c.startswith("mp4a."):
    return "AAC"
  if c.startswith("opus"):
    return "Opus"
  if c.startswith("ec-3") or c.startswith("ac-3"):
    return "Dolby Digital"
  return codecs or "audio"


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
