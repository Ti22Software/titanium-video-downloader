"""Rumble extractor (public v1): embedJS resolve + muxed-HLS tail.

Public flow, verified live cookie-less end to end::

  watch page (/v<key>-<slug>.html) → video key via oembed <link> tag
  → GET /embedJS/u3/?request=video&ver=2&v=<key>…
  → either ua.tar.* chunklists (muxed-HLS tail) or ua.mp4.* direct files
  → tar-wrapped chunklist.m3u8 → .ts segments → concat + ffmpeg remux,
    or single Range-resume GET (progressive, no ffmpeg)

  Shorts (/shorts/<id>) carry no oembed key — their application/json feed
  block holds the main video's mp4 rungs directly (scoped by permalink,
  sizes via HEAD), reusing the progressive tail.

Differences from the DASH path: renditions are muxed A/V (``muxed: True``,
``init_segment: None``), sizes are exact ``meta.size`` bytes (not
estimates), and there is a single shared AAC rendition. The ``a`` (ads)
block is never fetched — ad-skipping is inherent. Subtitle ``.vtt``
paths are present in embedJS; a future ``--write-subs`` is out of scope.
Livestreams (``live != 0``) fail closed. No login exists yet — public
only, like Vimeo Phase 2a.
"""

import re

import requests

from ..core.session import TIMEOUT, UA
from .base import AuthError, AuthProvider, _debug_redact_url, _host, fail

EMBEDJS_URL = ("https://rumble.com/embedJS/u3/?ifr=0&dref=rumble.com"
               "&request=video&ver=2&v={key}"
               "&ext=%7B%22ad_count%22%3Anull%7D&ad_wt=0")

# oEmbed discovery tags carry the key: rumble.com/embed/<key>/
_KEY_RE = re.compile(r"rumble\.com/embed/([A-Za-z0-9]+)", re.IGNORECASE)

# Watch URLs: /v<key>-<slug>.html, /embed/<key>/, and /shorts/<id> forms.
# Shorts pages carry no oembed key — their application/json feed block holds
# the main video's mp4 rungs directly (scoped by permalink/relative_url).
_WATCH_RE = re.compile(r"^/(?:v[A-Za-z0-9]+-|embed/[A-Za-z0-9]+/?$|shorts/[A-Za-z0-9]+/?$)",
                       re.IGNORECASE)

# Progressive suffix → height fallback when a rung reports no res label.
_SUFFIX_HEIGHT = {"baa": 360, "caa": 480, "gaa": 720,
                  "haa": 1080, "iaa": 1440, "jaa": 2160}


def _page_id(watch_url):
  """Shorts/watch page id from the URL path (shorts id or embed key)."""
  try:
    from urllib.parse import urlparse
    parts = [p for p in (urlparse(watch_url).path or "").split("/") if p]
  except Exception:
    return ""
  if not parts:
    return ""
  if parts[0].lower() == "shorts" and len(parts) > 1:
    return parts[1]
  if parts[0].lower() == "embed" and len(parts) > 1:
    return parts[1]
  return ""


def _is_shorts(watch_url):
  try:
    from urllib.parse import urlparse
    return (urlparse(watch_url).path or "").lower().startswith("/shorts/")
  except Exception:
    return False


def _watch_headers(watch_url):
  return {
    "User-Agent": UA,
    "Referer": watch_url,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.5",
    "Upgrade-Insecure-Requests": "1",
  }


def _api_headers(watch_url):
  return {
    "User-Agent": UA,
    "Referer": watch_url,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.5",
  }


def segment_headers(watch_url):
  """Segment-download headers (Referer-bound, like the browser's XHRs)."""
  return _api_headers(watch_url)


def _video_key(watch_html):
  m = _KEY_RE.search(watch_html or "")
  return m.group(1) if m else None


def _rung_height(entry):
  """Rung height: explicit res first, suffix map as fallback."""
  try:
    res = int(entry.get("res") or entry.get("resolution") or 0)
  except (TypeError, ValueError):
    res = 0
  if res:
    return res
  url = entry.get("url") or ""
  m = re.search(r"\.([a-z]{3})\.mp4(?:[?#]|$)", url)
  if m:
    return _SUFFIX_HEIGHT.get(m.group(1).lower(), 0)
  return 0


def _shorts_item(watch_html, watch_url, page_id):
  """Main-video item from a Shorts page's application/json feed blocks.

  Matches on relative_url, full url, or permalink_id == page id so related
  preloads never win. Falls back to the lone <source type="video/mp4">
  tag (single rung, suffix-derived height). fail()s when absent.
  """
  import json
  from urllib.parse import urlparse
  html = watch_html or ""
  try:
    want_path = urlparse(watch_url).path or ""
  except Exception:
    want_path = ""
  for m in re.finditer(r'<script type="application/json">(.*?)</script>',
                       html, re.S):
    try:
      doc = json.loads(m.group(1))
    except ValueError:
      continue
    items = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(items, list):
      continue
    for item in items:
      if not isinstance(item, dict):
        continue
      if (item.get("relative_url") == want_path
              or item.get("url") == watch_url
              or (page_id and item.get("permalink_id") == page_id)):
        return item
  m = re.search(r'<source\s+type="video/mp4"\s+src="([^"]+)"', html)
  if m:
    return {"title": None, "duration": None, "live": False,
            "videos": [{"url": m.group(1), "type": "mp4"}]}
  blocks = len(re.findall(r'<script type="application/json">', html))
  fail("could not find rumble short video data on page "
       f"(json_blocks={blocks} page={page_id!r}; removed video or page "
       "format changed).")


class RumbleAuth(AuthProvider):
  """Public Rumble: match real, login is a no-op, muxed-HLS resolve."""

  site_key = "rumble"
  requires_auth = False
  cookie_domains = ("rumble.com",)

  def __init__(self):
    # Per-watch-url bootstrap cache: (watch_html, embedjs_text). The CLI
    # calls resolve_embed then resolve_playlist back-to-back; the cache
    # avoids fetching twice. Keyed by URL so it stays correct.
    self._cache = {}

  def match(self, url):
    if _host(url) not in ("rumble.com", "www.rumble.com"):
      return False
    try:
      from urllib.parse import urlparse
      return bool(_WATCH_RE.match(urlparse(url).path or ""))
    except Exception:
      return False

  def login(self, session, email, password, debug=False):
    return session

  def browser_login(self, session, email, password, debug=False, headed=False):
    return session

  def _bootstrap(self, session, watch_url, debug=False):
    """Return (video_key, embedjs_dict), fetching each side once.

    rumble.com gates bare requests (Cloudflare 403s the watch page and
    embedJS alike), while hugh.cdn.rumble.cloud is open. So gated origin
    fetches go through one headless-Chromium page load (which clears the
    challenge and serves both the HTML and the in-page embedJS fetch),
    and all heavy CDN fetching stays in requests with resume support.
    """
    if watch_url in self._cache:
      return self._cache[watch_url]
    html = self._watch_html(session, watch_url)
    key = _video_key(html) if html is not None else None
    data = self._embedjs_json(session, watch_url, key) if key else None
    if html is None or data is None:
      key, html, data = self._browser_bundle(session, watch_url, key, debug)
    if not key:
      fail("could not find rumble video key on watch page "
           "(login-wall, removed video, or page format changed).")
    if data is None:
      fail("rumble embedJS unavailable (origin fetch failed).")
    if debug:
      import sys
      print(f"debug-login: rumble key={key}", file=sys.stderr)
    self._cache[watch_url] = (key, data)
    return key, data

  def _watch_html(self, session, watch_url):
    """Watch HTML via requests, or None when gated (Cloudflare)."""
    try:
      r = session.get(watch_url, headers=_watch_headers(watch_url), timeout=TIMEOUT)
    except requests.RequestException:
      return None
    return r.text if r.status_code == 200 else None

  def _embedjs_json(self, session, watch_url, key):
    """embedJS dict via requests, or None when gated."""
    try:
      r = session.get(EMBEDJS_URL.format(key=key),
                      headers=_api_headers(watch_url), timeout=TIMEOUT)
    except requests.RequestException:
      return None
    if r.status_code != 200:
      return None
    try:
      return r.json()
    except ValueError:
      return None

  def _browser_bundle(self, session, watch_url, key=None, debug=False):
    """One page load serving both origin sides: (key, html, embedjs|None).

    The page is primed on the watch URL (fresh context per call on the
    shared browser); content() yields the HTML and an in-page fetch
    inherits the page's referer/cookies/sec-fetch metadata for embedJS —
    byte-identical shape to the browser's own XHR (a detached API request
    misses fetch metadata and gets 403d). Cookies harvest into the
    session. A missing key is derived from the loaded HTML first, so a
    fully-gated video still resolves in a single load.
    """
    import json
    from ..core.browser import get_browser
    if debug:
      import sys
      print("debug-login: rumble origin gated, using browser bootstrap",
            file=sys.stderr)
    browser = get_browser()
    ctx = browser.new_context(user_agent=UA)
    page = ctx.new_page()
    try:
      from playwright.sync_api import TimeoutError as PwTimeout
    except ImportError:
      PwTimeout = TimeoutError
    try:
      try:
        page.goto(watch_url, wait_until="domcontentloaded", timeout=45000)
      except PwTimeout:
        pass
      html = page.content()
      for c in ctx.cookies():
        try:
          session.cookies.set(c["name"], c["value"],
                              domain=c.get("domain", ""),
                              path=c.get("path", "/"))
        except Exception:
          pass
      if key is None:
        key = _video_key(html)
      data = None
      if key is not None:
        try:
          got = page.evaluate(
            """async (url) => {
                 const r = await fetch(url, {headers: {"Accept": "*/*"}});
                 return {status: r.status, text: await r.text()};
               }""",
            EMBEDJS_URL.format(key=key))
        except Exception as e:
          raise AuthError(f"rumble browser fetch failed: {e}")
        if not isinstance(got, dict) or got.get("status") != 200:
          raise AuthError("rumble embedJS rejected in browser too "
                          f"(HTTP {got.get('status') if isinstance(got, dict) else '?'}).")
        try:
          data = json.loads(got["text"])
        except (ValueError, KeyError, TypeError):
          raise AuthError("rumble embedJS did not return JSON.")
      return key, html, data
    except AuthError:
      raise
    except Exception as e:
      raise AuthError(f"rumble browser bootstrap failed: {e}")
    finally:
      try:
        ctx.close()
      except Exception:
        pass

  def _resolve_shorts(self, session, watch_url, debug=False):
    """Shorts resolve: feed JSON on the page → progressive mp4 rungs.

    Scoped to the page's own video (permalink/relative_url match) so
    preloaded related shorts never leak in. Sizes via HEAD (CDN is open);
    a rung whose HEAD fails keeps size 0 and downloads unchecked.
    """
    html = self._watch_html(session, watch_url)
    if html is None:
      _key, html, _data = self._browser_bundle(session, watch_url, None, debug)
    page_id = _page_id(watch_url)
    item = _shorts_item(html, watch_url, page_id)
    if item.get("live"):
      fail("rumble livestreams are not supported (VOD only).")
    title = item.get("title") or "video"
    duration = item.get("duration")
    rungs = [e for e in (item.get("videos") or [])
             if isinstance(e, dict) and (e.get("type") or "mp4") == "mp4"
             and e.get("url")]
    if not rungs:
      keys = sorted(item.keys()) if isinstance(item, dict) else []
      fail("rumble short has no playable mp4 renditions "
           f"(item_keys={keys} page={page_id!r}).")
    pw, ph = item.get("video_width") or 0, item.get("video_height") or 0
    videos = []
    qmap = {}
    for e in sorted(rungs, key=lambda e: _rung_height(e), reverse=True):
      height = _rung_height(e)
      vid = f"rumble-{height}p"
      qmap[vid] = f"{height}p"
      width = int(round(height * pw / ph)) if pw and ph else None
      size = self._head_size(session, e["url"], watch_url, debug)
      videos.append({
        "id": vid,
        "width": width,
        "height": height,
        "bitrate": (e.get("bitrate_kbps") or 0) * 1000,
        "codecs": None,
        "duration": duration,
        "framerate": None,
        "init_segment": None,
        "base_url": "",
        "segments": [{"url": e["url"]}],
        "size_total": size,
        "muxed": True,
        "progressive": True,
      })
    videos.sort(key=lambda v: v["bitrate"], reverse=True)
    audios = [{
      "id": "rumble-audio",
      "bitrate": 0,
      "codecs": "mp4a.40.2",
      "mime_type": "audio/aac",
      "sample_rate": None,
      "channels": None,
      "init_segment": None,
      "base_url": "",
      # Progressive mp4s already carry audio — nothing to fetch.
      "segments": [],
    }]
    if debug:
      import sys
      print(f"debug-login: rumble short title={title[:60]!r} "
            f"duration={duration} renditions={len(videos)}",
            file=sys.stderr)
    return videos, audios, qmap, title

  def _head_size(self, session, url, referer, debug=False):
    """Content-Length via HEAD (open CDN), else 0 (unchecked download)."""
    try:
      r = session.head(url, headers=_api_headers(referer), timeout=TIMEOUT)
    except requests.RequestException:
      return 0
    if r.status_code != 200:
      return 0
    try:
      return int((r.headers or {}).get("Content-Length", 0))
    except (ValueError, TypeError):
      return 0

  def resolve_embed(self, session, watch_url, debug=False):
    """Return the embedJS URL (stable per video key) for --login-only.

    Shorts carry no oembed key (no embedJS) — the watch URL itself is
    the canonical reference.
    """
    if _is_shorts(watch_url):
      return watch_url
    key, _data = self._bootstrap(session, watch_url, debug)
    return EMBEDJS_URL.format(key=key)

  def resolve_playlist(self, session, watch_url, debug=False):
    """Full public resolve: watch → embedJS → muxed renditions + audio.

    Tar variant: video segments are absolute .ts URLs (chunklists fetched
    eagerly); progressive-mp4 variant: one direct .mp4 URL per rung
    (``progressive: True``) with empty audio segments. Shorts pages
    (``/shorts/<id>``) carry no oembed key — their feed JSON holds the
    main video's mp4 rungs (+ HEAD sizes) directly. Sizes are exact
    meta/HEAD bytes; tar audio is the single shared .aac direct file.
    """
    if _is_shorts(watch_url):
      return self._resolve_shorts(session, watch_url, debug)
    from ..core.fetcher import fetch_hls_chunklist
    _key, data = self._bootstrap(session, watch_url, debug)
    if data.get("live"):
      fail("rumble livestreams are not supported (VOD only).")
    title = data.get("title") or "video"
    duration = data.get("duration")
    fps = data.get("fps")
    ua = data.get("ua") or {}
    tar = ua.get("tar") or {}
    videos = []
    qmap = {}
    for label in ("1080", "720", "480", "360"):
      entry = tar.get(label)
      if not entry:
        continue
      meta = entry.get("meta") or {}
      chunklist_url = entry.get("url")
      if not chunklist_url:
        continue
      seg_urls = fetch_hls_chunklist(session, chunklist_url, watch_url)
      vid = f"rumble-{label}p"
      qmap[vid] = f"{label}p"
      videos.append({
        "id": vid,
        "width": meta.get("w"),
        "height": meta.get("h"),
        "bitrate": (meta.get("bitrate") or 0) * 1000,
        "codecs": None,
        "duration": duration,
        "framerate": fps,
        "init_segment": None,
        "base_url": "",
        "segments": [{"url": u} for u in seg_urls],
        "size_total": meta.get("size") or 0,
        "muxed": True,
      })
    if not videos:
      # Progressive-mp4 variant (no tar chunklists): ua.mp4 carries
      # complete muxed files, one URL per rung — all labels exposed.
      mp4s = ua.get("mp4") or {}

      def _rung_key(label):
        try:
          return int(label)
        except (TypeError, ValueError):
          return -1

      for label in sorted(mp4s, key=_rung_key, reverse=True):
        entry = mp4s.get(label) or {}
        meta = entry.get("meta") or {}
        mp4_url = entry.get("url")
        if not mp4_url:
          continue
        vid = f"rumble-{label}p"
        qmap[vid] = f"{label}p"
        videos.append({
          "id": vid,
          "width": meta.get("w"),
          "height": meta.get("h") or _rung_key(label),
          "bitrate": (meta.get("bitrate") or 0) * 1000,
          "codecs": None,
          "duration": duration,
          "framerate": fps,
          "init_segment": None,
          "base_url": "",
          "segments": [{"url": mp4_url}],
          "size_total": meta.get("size") or 0,
          "muxed": True,
          "progressive": True,
        })
    if not videos:
      tar_keys = sorted((tar or {}).keys())
      mp4_keys = sorted(((ua.get("mp4")) or {}).keys())
      fail("rumble embedJS has no playable renditions "
           f"(ua_keys={sorted(ua.keys())} tar_keys={tar_keys} "
           f"mp4_keys={mp4_keys} live={data.get('live')} "
           f"title={str(title)[:40]!r} duration={duration}).")
    videos.sort(key=lambda v: v["bitrate"], reverse=True)
    audio_entry = (ua.get("audio") or {}).get("192") or {}
    audio_meta = audio_entry.get("meta") or {}
    audio_url = audio_entry.get("url")
    progressive = bool(videos and videos[0].get("progressive"))
    audios = [{
      "id": "rumble-audio",
      "bitrate": (audio_meta.get("bitrate") or 0) * 1000,
      "codecs": "mp4a.40.2",
      "mime_type": "audio/aac",
      "sample_rate": None,
      "channels": None,
      "init_segment": None,
      "base_url": "",
      # Progressive mp4s already carry audio — nothing to fetch.
      "segments": [] if progressive else (
        [{"url": audio_url, "size": audio_meta.get("size") or 0}]
        if audio_url else []),
    }]
    if debug:
      import sys
      print(f"debug-login: rumble title={title[:60]!r} duration={duration} "
            f"renditions={len(videos)} audio_segs={len(audios[0]['segments'])} "
            f"progressive={progressive}",
            file=sys.stderr)
    return videos, audios, qmap, title
