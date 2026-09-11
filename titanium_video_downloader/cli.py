"""argparse front-end + orchestration (today's UI, prints intact).

Event bus (GUI prerequisite) is explicitly deferred — see ARCHITECTURE.md.
"""

import argparse
import atexit
import json
import shutil
import sys
from pathlib import Path

import requests

from .core.appconfig import (
  BUILTINS, default_config_path, effective_config, resolve_config)
from .core.batch import EntrySkip, dedupe_entries, is_url, parse_batch_file
from .core.browser import close_browser
from .core.disk import HEADROOM_BYTES, check_space, download_estimate
from .core.paths import user_path
from .core.envfile import load_dotenv
from .core.fetcher import download_direct, download_rendition, fetch_json
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

  Login is automatic, never a user decision: auth-required sites always
  harvest via browser, no-auth sites skip (explicit creds still reach
  login() so future real logins keep working). Pure decision helper
  (no I/O) so the matrix is unit-testable.
  """
  if args.no_auth:
    for opt in ("email", "password", "cookies"):
      if getattr(args, opt):
        fail(f"--no-auth conflicts with --{opt.replace('_', '-')}.")
    if prov.requires_auth:
      site = prov.site_key.upper()
      fail(f"{prov.site_key} requires auth — omit --no-auth or pass "
           f"--email/--password (TI22_VIDEO_DL_{site}_EMAIL).")
    return "none"
  if args.cookies:
    return "cookies"
  if prov.requires_auth:
    return "browser"
  return "password"


def _apply_noauth_override(args, explicit):
  """Explicit CLI login signals beat a layered config no_auth=true.

  Returns the effective no_auth. CLI --no-auth itself always wins (it is
  explicit); explicit --email/--password/--cookies override a config-file
  no_auth with a note. Pure (no I/O) for tests.
  """
  if "no_auth" in explicit:
    return bool(args.no_auth)
  if args.no_auth and any((args.email, args.password, args.cookies)):
    print("note: explicit login options override config no_auth=true; "
          "logging in", file=sys.stderr)
    return False
  return bool(args.no_auth)


def main(argv=None):
  ap = argparse.ArgumentParser(
    description="Download studygateway/vimeo/rumble videos to MP4 files.")
  ap.add_argument("inputs", nargs="*",
                  help="video URL(s). With one URL, an optional second positional "
                       "is the output path (legacy --output shorthand). With several "
                       "URLs, all must be http(s) (a batch — use --output-dir).")
  ap.add_argument("--batch-file", default=None,
                  help="text file of 'URL [QUALITY]' lines (# comments Ok); "
                       "combined with CLI URLs into one batch")
  ap.add_argument("--list-qualities", action="store_true",
                  help="list available renditions and exit")
  ap.add_argument("--list-qualities-json", action="store_true",
                  help="list renditions as a JSON array (front-end integration) and exit")
  pick_group = ap.add_mutually_exclusive_group()
  pick_group.add_argument("--pick", action="store_true", default=None,
                          help="interactively pick a quality from the list, then download")
  pick_group.add_argument("--no-pick", action="store_true", default=None,
                          help="never prompt for quality (overrides config pick_always)")
  ap.add_argument("--on-missing-quality",
                  choices=("fallback", "fail", "ask"), default=None,
                  help="when the requested quality doesn't exist: use nearest "
                       "below (fallback), fail, or ask with the full list "
                       "(no effect with --pick; config or default: fallback)")
  ap.add_argument("--ffmpeg-path", default=None,
                  help="explicit ffmpeg binary (default: imageio-ffmpeg extra, then PATH)")
  ap.add_argument("--quality", default=None,
                  help="height label (1080p/720p/540p/360p/240p), 'best', or rendition id prefix (config or default: best = highest rung)")
  ap.add_argument("--output", "-o", default=None, help="output MP4 path")
  ap.add_argument("--concurrency", "-j", type=int, default=None,
                  help="parallel segment downloads (config or default 4)")
  ap.add_argument("--keep-intermediate", action="store_true", default=None,
                  help="keep video/audio intermediates and parts dir")
  ap.add_argument("--email", default=None, help="login email (or TI22_VIDEO_DL_<SITE>_EMAIL)")
  ap.add_argument("--password", default=None, help="login password (or TI22_VIDEO_DL_<SITE>_PASSWORD)")
  ap.add_argument("--cookies", default=None, help="Netscape cookies.txt fallback for bot-blocked login (export logged-in browser cookies for .studygateway.com)")
  ap.add_argument("--login-only", action="store_true",
                  help="login + resolve embed URL, print it, and exit")
  ap.add_argument("--debug-login", action="store_true", default=None,
                  help="print redacted login diagnostics (urls, parse flags, cookies, challenge hits)")
  ap.add_argument("--headed", action="store_true", default=None,
                  help="show the browser window wherever one runs (login harvest, vimeo intercept)")
  ap.add_argument("--no-auth", action="store_true", default=None,
                  help="skip login entirely (only sites with requires_auth=False, e.g. public vimeo)")
  ap.add_argument("--output-dir", default=None,
                  help="directory for auto-named outputs and bare -o filenames "
                       "(dir-ful -o wins outright; default: cwd)")
  ap.add_argument("--temp-dir", default=None,
                  help="directory for intermediate .ti22 workdirs (fast local disk or RAM drive; default: beside output)")
  ap.add_argument("--no-space-check", action="store_true", default=None,
                  help="skip the pre-download disk-space gate (unreadable NAS/cloud drives)")
  ap.add_argument("--config", default=None,
                  help="explicit config.toml path (default: app config dir)")
  ap.add_argument("--print-config", action="store_true",
                  help="print the effective layered config (values + sources) and exit")
  args = ap.parse_args(argv)
  # An explicitly-passed but blank string flag is user error (often an empty
  # shell variable) — fail like a bad flag (exit 2), never silently default.
  for flag, value in (("--quality", args.quality),
                      ("--ffmpeg-path", args.ffmpeg_path),
                      ("--output", args.output),
                      ("--output-dir", args.output_dir),
                      ("--temp-dir", args.temp_dir),
                      ("--batch-file", args.batch_file),
                      ("--config", args.config),
                      ("--email", args.email),
                      ("--password", args.password),
                      ("--cookies", args.cookies)):
    if isinstance(value, str) and not value.strip():
      fail(f"{flag} needs a non-empty value (got blank).", code=2)
  config_path = user_path(args.config) if args.config else None
  explicit = {k for k in BUILTINS if getattr(args, k, None) is not None}
  args, config_sources = resolve_config(args, config_path=config_path)
  args.no_auth = _apply_noauth_override(args, explicit)
  if args.print_config:
    found = config_path if config_path else default_config_path()
    print(json.dumps(effective_config(args, config_sources, found), indent=2))
    return 0
  atexit.register(close_browser)
  urls = [p for p in args.inputs if is_url(p)]
  outs = [p for p in args.inputs if not is_url(p)]
  if len(outs) > 1:
    fail("only one positional output path is allowed "
         "(use --output-dir with batch inputs).")
  file_entries = parse_batch_file(args.batch_file) if args.batch_file else []
  if not urls and not file_entries:
    ap.error("no input URL: pass video URL(s) or --batch-file.")
  if outs and (file_entries or len(urls) > 1):
    fail("positional output is single-download only with batch inputs; "
         "use --output-dir.")
  if args.output and outs and args.output != outs[0]:
    fail("pass the output path either positionally or via --output, not both.")
  output_arg = args.output or (outs[0] if outs else None)
  if args.pick and (args.quality != "best" or args.list_qualities
                    or args.list_qualities_json):
    fail("--pick is mutually exclusive with "
         "--quality/--list-qualities/--list-qualities-json.")
  # After parse_args so --help exits without touching the environment.
  load_dotenv()  # ./.env, then <config-dir>/.env; real env always wins
  entries = file_entries + [(u, None) for u in urls]
  entries, dup_notes = dedupe_entries(entries)
  for note in dup_notes:
    print(note, file=sys.stderr)
  batch = len(entries) > 1
  if batch and (args.list_qualities or args.list_qualities_json):
    fail("--list-qualities modes are single-download only; "
         "drop them or pass one URL.")
  if batch and (args.output or outs):
    fail("--output/positional output is single-download only with batch "
         "inputs; use --output-dir.")

  session = new_session()

  if args.cookies:
    try:
      load_cookies(session, user_path(args.cookies))
    except AuthError as e:
      fail(str(e))

  if batch:
    return run_batch(args, entries, session)
  url, quality = entries[0]
  output_arg = args.output or (outs[0] if outs else None)
  output_dir = user_path(args.output_dir)
  temp_dir = user_path(args.temp_dir)
  try:
    bundle = _resolve_entry(session, args, url, quality, None, False, set(),
                            output_arg, output_dir, temp_dir)
  except EntrySkip:
    return 0
  if bundle["action"] != "download":
    return 0
  _download_entry(session, args, bundle, None, False)
  return 0


def run_batch(args, entries, session):
  """Sequential batch: resolve all → gate → download loop. Returns failures."""
  authed = set()
  output_dir = user_path(args.output_dir)
  temp_dir = user_path(args.temp_dir)
  total = len(entries)
  status = {}
  bundles = {}
  try:
    for i, (url, quality) in enumerate(entries):
      tag = f"[{i + 1}/{total}]"
      print(f"{tag} {url}", file=sys.stderr)
      try:
        bundle = _resolve_entry(session, args, url, quality, tag, True, authed,
                                None, output_dir, temp_dir)
      except EntrySkip as e:
        print(f"{tag} skipped: {e}", file=sys.stderr)
        status[i] = "skipped"
        continue
      except SystemExit:
        status[i] = "failed"
        continue
      except Exception as e:
        print(f"error: unexpected failure resolving {url}: {e}", file=sys.stderr)
        status[i] = "failed"
        continue
      if bundle["action"] != "download":
        status[i] = "ok"
        continue
      bundles[i] = bundle
    todo = [(i, bundles[i]) for i in sorted(bundles)]
    if todo and not args.no_space_check:
      for _, b in todo:
        b["workdir"].mkdir(parents=True, exist_ok=True)
        b["outdir"].mkdir(parents=True, exist_ok=True)
      ests = [download_estimate(b["video"], b["audio"]) for _, b in todo]
      if args.keep_intermediate:
        work_need = sum(ests) + HEADROOM_BYTES
      else:
        work_need = max(ests) + HEADROOM_BYTES
      out_need = sum(ests) + HEADROOM_BYTES
      b0 = todo[0][1]
      check_space(b0["workdir"], work_need, "batch temporary files")
      check_space(b0["outdir"], out_need, "batch outputs")
    for i, bundle in todo:
      tag = f"[{i + 1}/{total}]"
      try:
        _download_entry(session, args, bundle, tag, True)
      except SystemExit:
        status[i] = "failed"
        continue
      except Exception as e:
        print(f"error: unexpected failure downloading {bundle['url']}: {e}",
              file=sys.stderr)
        status[i] = "failed"
        continue
      status[i] = "ok"
  finally:
    close_browser()
  ok = sum(1 for s in status.values() if s == "ok")
  skipped = sum(1 for s in status.values() if s == "skipped")
  failed = sum(1 for s in status.values() if s == "failed")
  print(f"batch complete: {ok} ok, {skipped} skipped, {failed} failed",
        file=sys.stderr)
  for i, (url, _quality) in enumerate(entries):
    if status.get(i) != "ok":
      print(f"  {status.get(i)}: {url}", file=sys.stderr)
  return failed
def _resolve_entry(session, args, url, quality, tag, batch, authed,
                   output_arg, output_dir, temp_dir):
  """Resolve one video through selection. Returns a bundle dict.

  Bundle action: "download" (video/audio/urls/paths/est ready) |
  "loginonly" | "listed". Raises EntrySkip (exists/ask-skip);
  fail() exits (single path and batch per-entry failure alike).
  """
  pre = f"{tag} " if tag else ""
  input_url = url
  prov = provider_for(url)
  if prov is not None:
    mode = _auth_mode(args, prov)
    if prov.site_key in authed and mode in ("password", "browser"):
      if args.debug_login:
        print(f"debug-login: reusing {prov.site_key} session", file=sys.stderr)
    elif mode == "none":
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
      if prov.site_key == "vimeo" and args.headed:
        # --headed summons a visible browser: force the upfront intercept
        # harvest instead of fast-path-then-fallback.
        print(f"note: {prov.site_key} needs no login; browser will harvest "
              "the player config visibly", file=sys.stderr)
        prov.force_intercept = True
        prov.intercept_headed = True
      else:
        email, password, cred_source = get_creds(args, prov.site_key)
        if args.debug_login:
          print(f"debug-login: cred_source={cred_source} site={prov.site_key}",
                file=sys.stderr)
        if prov.site_key == "studygateway":
          # Requests SAML login was removed (reCAPTCHA walls it); logins
          # always harvest via browser.
          print("note: studygateway login uses browser harvest",
                file=sys.stderr)
        try:
          if mode == "browser":
            prov.browser_login(session, email, password,
                               debug=args.debug_login, headed=args.headed)
          else:
            prov.login(session, email, password, debug=args.debug_login)
        except AuthError as e:
          fail(f"{e}")
        print("login ok", file=sys.stderr)
        authed.add(prov.site_key)
    try:
      input_url = prov.resolve_embed(session, url, debug=args.debug_login)
    except SystemExit:
      raise
    except Exception as e:
      fail(f"could not resolve embed URL: {e}")
    print(f"embed: {input_url}", file=sys.stderr)
    if args.login_only:
      print(input_url)
      return {"action": "loginonly"}

  config_url, title, _vid = None, None, None
  if prov is not None and prov.site_key == "rumble":
    videos, audios, qmap, title = prov.resolve_playlist(
      session, url, debug=args.debug_login)
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
    return {"action": "listed"}

  if args.list_qualities_json:
    print(json.dumps(quality_items(videos, audios, qmap), indent=2))
    return {"action": "listed"}

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
    req_quality = quality if quality is not None else args.quality
    video, relation = find_video(videos, req_quality)
    if relation == "exact":
      pass
    elif args.on_missing_quality == "fail" or video is None:
      video = select_video(videos, req_quality)  # fails, established messages
    elif args.on_missing_quality == "fallback":
      print(f"note: quality '{req_quality}' not available — using "
            f"{label_for(video)} (nearest {relation}, "
            "--on-missing-quality=fallback)", file=sys.stderr)
    else:  # ask (same helper shape feeds per-video prompts in batch)
      if not sys.stdin.isatty():
        fail("--on-missing-quality=ask needs an interactive terminal "
             "(choose fallback or fail for scripts).")
      _print_rows()
      fb_idx = next(i for i, item in enumerate(rows, 1) if item["id"] == video["id"])
      sys.stderr.write(f"quality '{req_quality}' unavailable — pick "
                       f"[1-{len(rows)}], s to skip, Enter for "
                       f"{label_for(video)} fallback (--on-missing-quality=ask): ")
      sys.stderr.flush()
      choice = interpret_ask(sys.stdin.readline(), len(rows), fb_idx)
      if choice is None:
        print(f"note: skipped '{req_quality}' (--on-missing-quality=ask)",
              file=sys.stderr)
        raise EntrySkip(f"skipped '{req_quality}' by user choice")
      picked = rows[choice - 1]
      print(f"note: quality '{req_quality}' not available — using "
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
    if batch:
      print(f"note: output exists, skipping: {out_path}", file=sys.stderr)
      raise EntrySkip(f"output exists: {out_path}")
    fail(f"output exists: {out_path} (remove it or pass a different output path).")

  if temp_dir is not None:
    work_root = temp_dir
  else:
    work_root = out_path.parent
  workdir = work_root / (out_path.stem + ".ti22")
  est = download_estimate(video, audio)
  return {"action": "download", "url": url, "video": video, "audio": audio,
          "v_urls": v_urls, "a_urls": a_urls, "out_path": out_path,
          "outdir": out_path.parent, "workdir": workdir, "est": est, "q": q}


def _download_entry(session, args, bundle, tag, batch):
  """Download + mux one resolved bundle. Returns "ok". fail() exits."""
  pre = f"{tag} " if tag else ""
  video = bundle["video"]
  audio = bundle["audio"]
  v_urls = bundle["v_urls"]
  a_urls = bundle["a_urls"]
  out_path = bundle["out_path"]
  workdir = bundle["workdir"]
  workdir.mkdir(parents=True, exist_ok=True)
  print(f"note: {pre}output: {out_path.resolve()}", file=sys.stderr)
  print(f"note: {pre}temp: {workdir.resolve()}", file=sys.stderr)
  if not args.no_space_check:
    est = download_estimate(video, audio)
    check_space(workdir, est + HEADROOM_BYTES, "temporary files")
    check_space(out_path.parent, est + HEADROOM_BYTES, "output")
  fps = f" {video['framerate']:.2f}fps" if video.get("framerate") else ""
  q = bundle["q"]
  if video.get("progressive"):
    # Direct progressive file (e.g. Rumble mp4-only): single Range-resume
    # GET straight to the output — no segments, no ffmpeg, audio inside.
    size = video.get("size_total") or 0
    print(f"{pre}video: {q} {video['width']}x{video['height']}{fps} "
          f"(progressive mp4, {size / 1e6:.1f}M, audio included)",
          file=sys.stderr)
    url = v_urls[0] if v_urls else None
    if not url:
      fail("progressive rendition has no file URL.")
    download_direct("video", url, out_path, session,
                    headers=segment_headers(bundle["url"]),
                    expected_size=size or None)
  elif video.get("muxed"):
    # Muxed single-stream (e.g. HLS-TS): audio rides inside the segments.
    print(f"{pre}video: {q} {video['width']}x{video['height']}{fps} "
          f"({len(v_urls)} segs, muxed A/V)", file=sys.stderr)
    download_rendition("video", video, v_urls, workdir, session,
                       args.concurrency, suffix=".ts", assemble=False,
                       headers=segment_headers(bundle["url"]))
    remux_concat(workdir / "video", out_path, ffmpeg=args.ffmpeg_path)
  else:
    sr = audio.get("sample_rate")
    sr_label = f" {sr / 1000:.1f}kHz" if sr else ""
    print(f"{pre}video: {q} {video['width']}x{video['height']}{fps} "
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
  return "ok"
