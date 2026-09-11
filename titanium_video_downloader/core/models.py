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


def find_video(videos, quality):
  """Non-failing selection: return (video|None, relation).

  relation is "exact" (best/height/id-prefix hit), "below"/"above" (nearest
  rung around a numeric miss, below preferred), or "missing" (non-numeric
  miss — no rung to order against, so no fallback possible). Batch reuse:
  same helper feeds per-video resolution later.
  """
  if quality in ("best", "highest", "max"):
    return videos[0], "exact"
  m = re.fullmatch(r"(\d+)\s*p?", quality.strip().lower())
  if m:
    want = int(m.group(1))
    for v in videos:
      if v.get("height") == want:
        return v, "exact"
    ordered = order_by_height(videos)
    below = [v for v in ordered if (v.get("height") or 0) < want]
    if below:
      return below[0], "below"
    above = [v for v in ordered if (v.get("height") or 0) > want]
    if above:
      return above[-1], "above"
    return None, "missing"
  for v in videos:
    if v.get("id", "").startswith(quality):
      return v, "exact"
  return None, "missing"


def interpret_ask(raw, n, fallback_idx):
  """Parse an ask-mode answer: 1-based index, s(kip), or "" for fallback.

  Pure (stdin injected) so batch reuse and tests share it. fail()s on
  garbage, like pick_index.
  """
  text = (raw or "").strip().lower()
  if text == "":
    return fallback_idx
  if text in ("s", "skip"):
    return None
  return pick_index(n, text)


def select_audio(audios):
  aac = [a for a in audios if (a.get("codecs") or "").startswith("mp4a.")]
  pool = aac or audios
  pool = sorted(pool, key=lambda a: a["bitrate"], reverse=True)
  return pool[0]


def resolve_segments(playlist_url, rendition):
  base = urljoin(playlist_url, rendition["base_url"])
  return [urljoin(base, s["url"]) for s in rendition["segments"]]


def order_by_height(videos):
  """Height-desc ordering shared by the table, JSON, and --pick list."""
  return sorted(videos, key=lambda v: v.get("height") or 0, reverse=True)


def video_bytes(video):
  """Exact size_total when the backend reports one, else segment sum."""
  if video.get("size_total") is not None:
    return video["size_total"]
  return sum(s.get("size", 0) for s in video["segments"])


def audio_bytes(audio):
  return sum(s.get("size", 0) for s in audio["segments"])


def quality_items(videos, audios, qmap):
  """Machine-readable rendition rows for --list-qualities-json.

  size is the estimated TOTAL download (video rung + the selected audio
  rendition, via the same select_audio() the download path uses), so the
  numbers match what lands on disk. A rendition may carry an exact
  size_total (e.g. backend-reported bytes), which wins over segment sums.
  Container (moov) overhead — KBs on GB files — is excluded and documented
  as such.
  """
  audio = select_audio(audios)
  audio_total = audio_bytes(audio)
  items = []
  for v in order_by_height(videos):
    video_total = video_bytes(v)
    items.append({
      "id": v.get("id"),
      "quality": qmap.get(v["id"], label_for(v)),
      "width": v.get("width"),
      "height": v.get("height"),
      "bitrate": v.get("bitrate") or 0,
      "size": video_total + audio_total,
      "video_size": video_total,
      "audio_size": audio_total,
      "codecs": v.get("codecs"),
    })
  return items


def pick_index(n, raw):
  """Parse a 1-based --pick choice ("" means default 1), or fail()."""
  text = (raw or "").strip()
  if text == "":
    return 1
  try:
    idx = int(text)
  except ValueError:
    fail(f"invalid pick '{text}' (enter 1-{n}).")
  if not 1 <= idx <= n:
    fail(f"pick {idx} out of range (1-{n}).")
  return idx
