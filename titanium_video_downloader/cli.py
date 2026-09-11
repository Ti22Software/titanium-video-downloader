"""argparse front-end + orchestration (today's UI, prints intact).

Event bus (GUI prerequisite) is explicitly deferred — see ARCHITECTURE.md.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

import requests

from .core.disk import HEADROOM_BYTES, check_space, download_estimate
from .core.paths import user_path
from .core.envfile import load_dotenv
from .core.fetcher import download_rendition, fetch_json
from .core.models import (
  audio_label,
  find_video,
  interpret_ask,
  label_for,
  parse_playlist,
  pick_index,
  quality_items,
  resolve_segments,
  select_audio,
  select_video,
)
from .core.mux import mux, remux_concat
from .core.naming import sanitize, sanitize_path
from .core.session import (
  EMBED_ORIGIN_HEADERS,
  TIMEOUT,
  get_creds,
  load_cookies,
  new_session,
)
from .extractors.base import AuthError, fail, provider_for, register_provider
from .extractors.generic import GenericAuth
from .extractors.rightnowmedia import RightNowMediaAuth
from .extractors.rumble import RumbleAuth, segment_headers
from .extractors.studygateway import pick_playlist_url, resolve_config_url, StudyGatewayAuth
from .extractors.vimeo import VimeoAuth

register_provider(StudyGatewayAuth())
register_provider(VimeoAuth())
register_provider(RightNowMediaAuth())
register_provider(RumbleAuth())
register_provider(GenericAuth())


def _env_creds_present(site_key):
  """True if any credential env for the site is configured."""
  import os
  key = (site_key or "").upper()
  names = [f"TI22_VIDEO_DL_{key}_EMAIL", f"TI22_VIDEO_DL_{key}_PASSWORD"] if key else []
  return any(os.environ.get(n) for n in names)


def _auth_mode(args, prov):
  """Return 'none'|'cookies'|'browser'|'password', or fail() on conflicts.

  Pure decision helper (no I/O) so the matrix is unit-testable.
  """
  if args.no_auth:
    for opt in ("email", "password", "cookies", "use_browser_login"):
      if getattr(args, opt):
        fail(f"--no-auth conflicts with --{opt.replace('_', '-')}.")
    if prov.requires_auth:
      site = prov.site_key.upper()
      fail(f"{prov.site_key} requires auth — omit --no-auth or pass "
           f"--email/--password (TI22_VIDEO_DL_{site}_EMAIL).")
    return "none"
  if args.cookies and not args.use_browser_login:
    return "cookies"
  if args.use_browser_login:
    return "browser"
  return "password"


def main(argv=None):
  ap = argparse.ArgumentParser(
    description="Download a single studygateway video to an MP4 file.")
  ap.add_argument("input_url", help="watch.studygateway.com video URL, embed.vhx.tv iframe URL, or player.vimeo.com config URL")
  ap.add_argument("output_pos", nargs="?", default=None,
                  help="output MP4 path (shorthand for --output)")
  ap.add_argument("--list-qualities", action="store_true",
                  help="list available renditions and exit")
  ap.add_argument("--list-qualities-json", action="store_true",
                  help="list renditions as a JSON array (front-end integration) and exit")
  ap.add_argument("--pick", action="store_true",
                  help="interactively pick a quality from the list, then download")
  ap.add_argument("--on-missing-quality",
                  choices=("fallback", "fail", "ask"), default="fallback",
                  help="when the requested quality doesn't exist: use nearest "
                       "below (fallback), fail, or ask with the full list "
                       "(no effect with --pick)")
  ap.add_argument("--ffmpeg-path", default=None,
                  help="explicit ffmpeg binary (default: imageio-ffmpeg extra, then PATH)")
  ap.add_argument("--quality", default="best",
                  help="height label (1080p/720p/540p/360p/240p), 'best', or rendition id prefix")
  ap.add_argument("--output", "-o", default=None, help="output MP4 path")
  ap.add_argument("--concurrency", "-j", type=int, default=4,
                  help="parallel segment downloads (default 4)")
  ap.add_argument("--keep-intermediate", action="store_true",
                  help="keep video/audio intermediates and parts dir")
  ap.add_argument("--email", default=None, help="login email (or TI22_VIDEO_DL_<SITE>_EMAIL)")
  ap.add_argument("--password", default=None, help="login password (or TI22_VIDEO_DL_<SITE>_PASSWORD)")
  ap.add_argument("--cookies", default=None, help="Netscape cookies.txt fallback for bot-blocked login (export logged-in browser cookies for .studygateway.com)")
  ap.add_argument("--login-only", action="store_true",
                  help="login + resolve embed URL, print it, and exit")
  ap.add_argument("--debug-login", action="store_true",
                  help="print redacted login diagnostics (urls, parse flags, cookies, challenge hits)")
  ap.add_argument("--use-browser-login", action="store_true",
                  help="real Chromium login via Playwright (passes reCAPTCHA v3); exports cookies to pipeline")
  ap.add_argument("--headed", action="store_true",
                  help="show browser window with --use-browser-login (debug 2FA/CAPTCHA)")
  ap.add_argument("--no-auth", action="store_true",
                  help="skip login entirely (only sites with requires_auth=False, e.g. public vimeo)")
  ap.add_argument("--output-dir", default=None,
                  help="directory for auto-named outputs and bare -o filenames "
                       "(dir-ful -o wins outright; default: cwd)")
  ap.add_argument("--temp-dir", default=None,
                  help="directory for intermediate .ti22 workdirs (fast local disk or RAM drive; default: beside output)")
  ap.add_argument("--no-space-check", action="store_true",
                  help="skip the pre-download disk-space gate (unreadable NAS/cloud drives)")
  args = ap.parse_args(argv)
  if args.output and args.output_pos and args.output != args.output_pos:
    fail("pass the output path either positionally or via --output, not both.")
  if args.pick and (args.quality != "best" or args.list_qualities
                    or args.list_qualities_json):
    fail("--pick is mutually exclusive with "
         "--quality/--list-qualities/--list-qualities-json.")
  # After parse_args so --help exits without touching the environment.
  load_dotenv()  # ./.env, then <config-dir>/.env; real env always wins
  output_arg = args.output or args.output_pos
  output_dir = user_path(args.output_dir)
  temp_dir = user_path(args.temp_dir)

  session = new_session()

  if args.cookies:
    try:
      load_cookies(session, user_path(args.cookies))
    except AuthError as e:
      fail(str(e))

  input_url = args.input_url
  prov = provider_for(input_url)
  if prov is not None:
    mode = _auth_mode(args, prov)
    if mode == "none":
      if _env_creds_present(prov.site_key):
        print("note: ignoring configured credentials due to --no-auth", file=sys.stderr)
      print(f"note: --no-auth accepted for {prov.site_key}, skipping login", file=sys.stderr)
    elif mode == "cookies":
      print("note: using --cookies session, skipping password login", file=sys.stderr)
    elif mode == "password" and not prov.requires_auth and not (args.email or args.password):
      # No-auth-capable site, no creds given: skip the login call instead of
      # printing a "login ok" no authentication earned. (Explicit creds fall
      # through to login() so future real logins keep working.)
      print(f"note: {prov.site_key} needs no login, skipping", file=sys.stderr)
    else:
      if mode == "browser" and args.cookies:
        print("note: --cookies ignored with --use-browser-login (browser session wins)",
              file=sys.stderr)
      if mode == "browser" and prov.site_key == "vimeo":
        # Vimeo login is a no-op; the browser is wanted for config harvest
        # at resolve time instead. Flagged on the provider (documented hook).
        # No "login ok" here either — nothing authenticated (yet).
        print(f"note: {prov.site_key} needs no login; browser will harvest "
              "the player config", file=sys.stderr)
        prov.force_intercept = True
        prov.intercept_headed = args.headed
      else:
        email, password, cred_source = get_creds(args, prov.site_key)
        if args.debug_login:
          print(f"debug-login: cred_source={cred_source} site={prov.site_key}",
                file=sys.stderr)
        if prov.site_key == "studygateway" and mode == "password":
          # Requests SAML login was removed (reCAPTCHA walls it); password
          # logins auto-upgrade to the browser harvest, same as the flag.
          print("note: studygateway password login uses browser harvest",
                file=sys.stderr)
          mode = "browser"
        try:
          if mode == "browser":
            prov.browser_login(session, email, password,
                               debug=args.debug_login, headed=args.headed)
          else:
            prov.login(session, email, password, debug=args.debug_login)
        except AuthError as e:
          fail(f"{e}")
        print("login ok", file=sys.stderr)
    try:
      input_url = prov.resolve_embed(session, args.input_url, debug=args.debug_login)
    except SystemExit:
      raise
    except Exception as e:
      fail(f"could not resolve embed URL: {e}")
    print(f"embed: {input_url}", file=sys.stderr)
    if args.login_only:
      print(input_url)
      return 0

  config_url, title, _vid = None, None, None
  if prov is not None and prov.site_key == "rumble":
    videos, audios, qmap, title = prov.resolve_playlist(
      session, args.input_url, debug=args.debug_login)
    playlist_url = ""
    config = {}
  else:
    config_url, title, _vid = resolve_config_url(session, input_url)
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
    for item in quality_items(videos, audios, qmap):
      print(f"{item['quality']:<8}{item['size'] / 1e6:>9.1f}M  "
            f"{item['bitrate']:>9}  {item['id']}")
    return 0

  if args.list_qualities_json:
    print(json.dumps(quality_items(videos, audios, qmap), indent=2))
    return 0

  rows = quality_items(videos, audios, qmap)

  def _print_rows():
    for i, item in enumerate(rows, 1):
      print(f"  [{i}] {item['quality']:<8}{item['size'] / 1e6:>9.1f}M  "
            f"{item['bitrate']:>9}  {item['id']}", file=sys.stderr)

  if args.pick:
    if not sys.stdin.isatty():
      fail("--pick needs an interactive terminal.")
    _print_rows()
    sys.stderr.write(f"pick quality [1-{len(rows)}] (default 1): ")
    sys.stderr.flush()
    picked = rows[pick_index(len(rows), sys.stdin.readline()) - 1]
    video = next(v for v in videos if v["id"] == picked["id"])
  else:
    video, relation = find_video(videos, args.quality)
    if relation == "exact":
      pass
    elif args.on_missing_quality == "fail" or video is None:
      video = select_video(videos, args.quality)  # fails, established messages
    elif args.on_missing_quality == "fallback":
      print(f"note: quality '{args.quality}' not available — using "
            f"{label_for(video)} (nearest {relation}, "
            "--on-missing-quality=fallback)", file=sys.stderr)
    else:  # ask (batch reuse: same helper shape feeds per-video prompts later)
      if not sys.stdin.isatty():
        fail("--on-missing-quality=ask needs an interactive terminal "
             "(choose fallback or fail for scripts).")
      _print_rows()
      fb_idx = next(i for i, item in enumerate(rows, 1) if item["id"] == video["id"])
      sys.stderr.write(f"quality '{args.quality}' unavailable — pick "
                       f"[1-{len(rows)}], s to skip, Enter for "
                       f"{label_for(video)} fallback (--on-missing-quality=ask): ")
      sys.stderr.flush()
      choice = interpret_ask(sys.stdin.readline(), len(rows), fb_idx)
      if choice is None:
        print(f"note: skipped '{args.quality}' (--on-missing-quality=ask)",
              file=sys.stderr)
        return 0
      picked = rows[choice - 1]
      print(f"note: quality '{args.quality}' not available — using "
            f"{picked['quality']} (picked, --on-missing-quality=ask)",
            file=sys.stderr)
      video = next(v for v in videos if v["id"] == picked["id"])
  audio = select_audio(audios)
  v_urls = resolve_segments(playlist_url or "", video)
  a_urls = resolve_segments(playlist_url or "", audio)

  name = sanitize(title or config.get("video", {}).get("title") or "video")
  q = qmap.get(video["id"], label_for(video))
  if output_arg:
    # Harmonious -o x --output-dir: a bare filename joins under the dir
    # (batch-friendly: regex can name files later); a dir-ful -o wins
    # outright and the dir flag is reported ignored, never silently.
    given = user_path(output_arg)
    if given.parent == Path(".") and output_dir is not None:
      out_path = sanitize_path(output_dir / given.name)
    else:
      out_path = sanitize_path(given)
      if output_dir is not None:
        print(f"note: --output-dir ignored (dir-ful -o wins): {output_dir}",
              file=sys.stderr)
    if Path(output_arg).name != out_path.name:
      print(f"note: sanitized output name to '{out_path.name}'", file=sys.stderr)
  else:
    if output_dir is not None:
      output_dir.mkdir(parents=True, exist_ok=True)
      out_path = output_dir / f"{name} [{q}].mp4"
    else:
      out_path = Path(f"{name} [{q}].mp4")
  if out_path.suffix.lower() != ".mp4":
    out_path = out_path.with_suffix(".mp4")
    print(f"note: using output name '{out_path.name}'", file=sys.stderr)
  out_path.parent.mkdir(parents=True, exist_ok=True)
  if out_path.exists():
    fail(f"output exists: {out_path} (remove it or pass a different output path).")

  if temp_dir is not None:
    work_root = temp_dir
  else:
    work_root = out_path.parent
  workdir = work_root / (out_path.stem + ".ti22")
  workdir.mkdir(parents=True, exist_ok=True)
  print(f"note: output: {out_path.resolve()}", file=sys.stderr)
  print(f"note: temp: {workdir.resolve()}", file=sys.stderr)
  if not args.no_space_check:
    est = download_estimate(video, audio)
    check_space(workdir, est + HEADROOM_BYTES, "temporary files")
    check_space(out_path.parent, est + HEADROOM_BYTES, "output")
  fps = f" {video['framerate']:.2f}fps" if video.get("framerate") else ""
  if video.get("muxed"):
    # Muxed single-stream (e.g. HLS-TS): audio rides inside the segments.
    print(f"video: {q} {video['width']}x{video['height']}{fps} "
          f"({len(v_urls)} segs, muxed A/V)", file=sys.stderr)
    download_rendition("video", video, v_urls, workdir, session,
                       args.concurrency, suffix=".ts", assemble=False,
                       headers=segment_headers(args.input_url))
    remux_concat(workdir / "video", out_path, ffmpeg=args.ffmpeg_path)
  else:
    sr = audio.get("sample_rate")
    sr_label = f" {sr / 1000:.1f}kHz" if sr else ""
    print(f"video: {q} {video['width']}x{video['height']}{fps} "
          f"({len(v_urls)} segs), "
          f"audio: {audio_label(audio.get('codecs'))}{sr_label} ({len(a_urls)} segs)",
          file=sys.stderr)
    v_path = download_rendition("video", video, v_urls, workdir, session, args.concurrency)
    a_path = download_rendition("audio", audio, a_urls, workdir, session, args.concurrency)
    mux(v_path, a_path, out_path, total_duration=video.get("duration"),
        ffmpeg=args.ffmpeg_path)
  if not args.keep_intermediate:
    shutil.rmtree(workdir, ignore_errors=True)
  print(str(out_path))
  return 0
