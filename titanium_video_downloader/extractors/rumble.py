"""Rumble extractor (public v1): embedJS resolve + muxed-HLS tail.

Public flow, verified live cookie-less end to end::

  watch page (/v<key>-<slug>.html) → video key via oembed <link> tag
  → GET /embedJS/u3/?request=video&ver=2&v=<key>…
  → ua.tar.{360,480,720,1080} chunklist URLs + meta{bitrate,size,w,h}
  → ua.audio.192 direct .aac + meta
  → tar-wrapped chunklist.m3u8 → .ts segments → concat + ffmpeg remux

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

# Watch URLs: /v<key>-<slug>.html and /embed/<key>/ forms.
_WATCH_RE = re.compile(r"^/(?:v[A-Za-z0-9]+-|embed/[A-Za-z0-9]+/?$)", re.IGNORECASE)


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


class RumbleAuth(AuthProvider):
  """Public Rumble: match real, login is a no-op, muxed-HLS resolve."""

  site_key = "rumble"
  requires_auth = False

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
    embedJS alike), while hugh.cdn.rumble.cloud is open. So origin fetches
    go through headless Chromium (which clears the challenge), and all
    heavy CDN fetching stays in requests with resume support.
    """
    if watch_url in self._cache:
      return self._cache[watch_url]
    html = self._watch_html(session, watch_url)
    if html is None:
      html = self._browser_html(session, watch_url, debug)
    key = _video_key(html)
    if not key:
      fail("could not find rumble video key on watch page "
           "(login-wall, removed video, or page format changed).")
    data = self._embedjs_json(session, watch_url, key)
    if data is None:
      data = self._browser_json(session, watch_url, key, debug)
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

  def _browser_pages(self, session, watch_url, debug=False):
    """Headless Chromium session primed on the watch page (yields page)."""
    try:
      from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
      raise AuthError("playwright not installed — run: pip install playwright "
                      "&& playwright install chromium, or use --cookies.")
    if debug:
      import sys
      print("debug-login: rumble origin gated, using browser bootstrap",
            file=sys.stderr)
    pw = sync_playwright().start()
    browser = pw.chromium.launch()
    ctx = browser.new_context(user_agent=UA)
    page = ctx.new_page()
    try:
      page.goto(watch_url, wait_until="domcontentloaded", timeout=45000)
    except PwTimeout:
      pass
    return pw, browser, ctx, page

  def _browser_html(self, session, watch_url, debug=False):
    pw, browser, ctx, page = self._browser_pages(session, watch_url, debug)
    try:
      html = page.content()
      for c in ctx.cookies():
        try:
          session.cookies.set(c["name"], c["value"],
                              domain=c.get("domain", ""),
                              path=c.get("path", "/"))
        except Exception:
          pass
      return html
    finally:
      try:
        ctx.close()
      except Exception:
        pass
      try:
        browser.close()
      except Exception:
        pass
      try:
        pw.stop()
      except Exception:
        pass

  def _browser_json(self, session, watch_url, key, debug=False):
    import json
    pw, browser, ctx, page = self._browser_pages(session, watch_url, debug)
    try:
      # In-page fetch (not page.request): inherits the page's referer,
      # cookies, and sec-fetch-mode:cors/site:same-origin — byte-identical
      # shape to the browser's own embedJS XHR. A detached API request
      # misses fetch metadata and gets 403d.
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
        return json.loads(got["text"])
      except (ValueError, KeyError, TypeError):
        raise AuthError("rumble embedJS did not return JSON.")
    except AuthError:
      raise
    except Exception as e:
      raise AuthError(f"rumble browser bootstrap failed: {e}")
    finally:
      try:
        ctx.close()
      except Exception:
        pass
      try:
        browser.close()
      except Exception:
        pass
      try:
        pw.stop()
      except Exception:
        pass

  def resolve_embed(self, session, watch_url, debug=False):
    """Return the embedJS URL (stable per video key) for --login-only."""
    key, _data = self._bootstrap(session, watch_url, debug)
    return EMBEDJS_URL.format(key=key)

  def resolve_playlist(self, session, watch_url, debug=False):
    """Full public resolve: watch → embedJS → muxed renditions + audio.

    Returns (videos, audios, qmap, title). Video segments are absolute
    .ts URLs (chunklists fetched eagerly — 4 small tar GETs); sizes are
    exact meta bytes; audio is the single shared .aac direct file.
    """
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
      fail("rumble embedJS has no playable renditions.")
    videos.sort(key=lambda v: v["bitrate"], reverse=True)
    audio_entry = (ua.get("audio") or {}).get("192") or {}
    audio_meta = audio_entry.get("meta") or {}
    audio_url = audio_entry.get("url")
    audios = [{
      "id": "rumble-audio",
      "bitrate": (audio_meta.get("bitrate") or 0) * 1000,
      "codecs": "mp4a.40.2",
      "mime_type": "audio/aac",
      "sample_rate": None,
      "channels": None,
      "init_segment": None,
      "base_url": "",
      "segments": [{"url": audio_url, "size": audio_meta.get("size") or 0}] if audio_url else [],
    }]
    if debug:
      import sys
      print(f"debug-login: rumble title={title[:60]!r} duration={duration} "
            f"renditions={len(videos)} audio_segs={len(audios[0]['segments'])}",
            file=sys.stderr)
    return videos, audios, qmap, title
