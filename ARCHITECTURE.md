# Titanium Video Downloader — Architecture (living doc)

Multi-site streaming video downloader (single downloads + batch), growing
into a GUI-capable app with downloadable executables for non-tech users.
Release binary name: `ti22-video-dl` (`ti22-video-dl.exe` on Windows).

## Current state (v0.1.0, verified working)

`ti22-video-dl` downloads StudyGateway, Vimeo, and Rumble videos (watch,
shorts, embeds) to MP4, singly or in batch with a per-video ledger.
Verified live: ~800MB StudyGateway download (ffprobe specs, null-decode,
VLC playback, md5-determinism across runs), 6-file mixed-provider batches
(6 ok), Rumble tar + progressive tails, Shorts end to end.
Determinism is per-binary: same ffmpeg build → byte-identical; across builds
the MP4 writer tag alone differs (e.g. `Lavf60.16.100` vs `Lavf61.1.100` =
1 byte on a 746MB file), content-equivalent either way.
StudyGateway login is browser-or-cookies only: pure-requests SAML login was
removed (the server demands a reCAPTCHA v3 token no script can mint, and the
path once printed false `login ok` — verified against the live public browse
page, which carries all the old auth-marker substrings with null user ids).
Auth-required sites always harvest via browser (there is no password mode
to upgrade from anymore); bad credentials fail in ~10s naming the site's
rejection instead of the old 90s blind wait.

### Pipeline: StudyGateway (4 hops)

```
watch.studygateway.com/.../videos/<slug>  (auto-login: --email/--password
  or TI22_VIDEO_DL_STUDYGATEWAY_EMAIL/PASSWORD feed the browser harvest;
  --cookies Netscape fallback; --login-only test)
  → browser SAML login (Chromium passes reCAPTCHA v3 natively, follows
    saml/consume → browse?ticket=, exports _session)
  → verify via _current_user/logout markers (browse is public 200 either
    way; cookie presence proves nothing)
  → slug page (Referer: .../browse, never self) → tokenized iframe
    embed.vhx.tv/videos/<vhx_id>?auth-user-token=<hours-lived JWT>
    (generic iframe without token 401s; parse order: tokenized iframe →
    window.VHX.config.embed_url → bare token join → generic fallback)
  → window.OTTData.config_url in embed HTML
  → player.vimeo.com/video/<clip_id>/config?token=<60-SEC JWT>
  → request.files.dash.cdns[default_cdn].avc_url
  → vod-adaptive-ak.vimeocdn.com/.../playlist.json (~1.5h signed URL)
  → init_segment (inline base64) + ~224 segment GETs each for video + audio
  → ffmpeg -c copy mux → out.mp4
```

### Pipeline: Vimeo (fast path + intercept fallback)

```
vimeo.com/<clip_id> → bare player config GET (lax videos, one round-trip)
  → playlist.json → DASH video + audio segments → ffmpeg mux → out.mp4.
  On 401/403/410 (or --headed): headless watch-page Play-click intercept
  captures the player's own minted config (h=/s=), then the same tail.
```

### Pipeline: Rumble (embedJS, two tails + Shorts)

```
rumble.com/v<key>-<slug>.html → video key (oembed tag)
  → embedJS JSON (gated origin via one shared-browser page load; CDN open)
  → tar variant: chunklist URLs → .ts segments → concat + ffmpeg remux
  → progressive-mp4 variant: direct .mp4 per quality level, Range-resume
    GET, no ffmpeg, audio inside (empty audio rendition).
rumble.com/shorts/<id> → page feed JSON (no oembed key; scoped by
  permalink; HEAD sizes) → same progressive tail.
```

### Token lifetimes, StudyGateway (drive ordering + error handling)

| Token             | Lifetime | Failure mode                    |
| ----------------- | -------- | ------------------------------- |
| Embed auth-token  | hours    | Embed page 401/403 → re-paste   |
| Config token      | ~60s     | Config fetch 410 → "token expired" |
| Playlist URL      | ~5380s   | Segment 401/403/410 → re-resolve |

Pasted config URLs are inherently fragile (browser may have consumed the
single-use token; 60s expires while copy-pasting). The reliable inputs are the
**watch slug URL** (browser login mints its own embed/config tokens in
seconds) or the **embed URL** — both minted-and-consumed live.
Login paths: browser harvest via Playwright Chromium (passes reCAPTCHA v3
natively; needs `pip install playwright && playwright install chromium`;
automatic for auth-required sites — login is never a user decision),
`--cookies` exclusive Netscape export (per-site jar truth-check decides
which sites use it) or per-site `TI22_VIDEO_DL_<SITE>_COOKIES` env gated
by `--prefer-cookies`/`prefer_cookies` (debug/dev; default off; stale
entries warn at load, failures name re-export). `--no-auth` skips login
entirely on
sites with `requires_auth=False` (public vimeo); auth-required sites fail
fast naming the site. See the multi-site table below.

Credentials (precedence: flags > site env > tty prompt; no generic
fallback by design — app-qualified names keep shared scopes collision-free):
`--email/--password`, `TI22_VIDEO_DL_<SITE>_EMAIL/PASSWORD`
(`TI22_VIDEO_DL_STUDYGATEWAY_EMAIL`, `TI22_VIDEO_DL_VIMEO_EMAIL`,
`TI22_VIDEO_DL_RIGHTNOWMEDIA_EMAIL`, `TI22_VIDEO_DL_GENERIC_EMAIL`),
plus per-site `TI22_VIDEO_DL_<SITE>_COOKIES` files (debug/dev; used only
with `--prefer-cookies`/`prefer_cookies`).
Optional `.env` (`./.env`, then
`~/.config/titanium-software/ti22-video-dl/.env`,
`%APPDATA%/titanium-software/ti22-video-dl/.env`, overridable via
`TI22_VIDEO_DL_CONFIG_DIR`): flat stdlib-parsed `KEY=VALUE`, real env always wins.

Quality selection: `--quality` (label/`best`/id-prefix, default `best` =
literal highest video quality, e.g. 2160p where published),
`--list-qualities` (human table, stdout), `--list-qualities-json` (JSON
array for front-ends, stdout), `--pick`/`--no-pick` (TTY picker forcing
picking on/off against config `pick_always`; `--pick` mutually exclusive
with the list modes).
Missing quality level + `--on-missing-quality` (default `fallback`): exact hits pass
silently; `fallback` takes nearest-below (nearest-above at the floor) with
an announcing note; `fail` keeps the legacy error; `ask` offers the full
numbered list plus skip on TTY, refuses on non-TTY. Non-numeric misses fail
in all modes. `--pick` bypasses the flag (a chosen quality cannot miss).
`size` everywhere means estimated TOTAL download = selected video quality +
`select_audio()` rendition (container `moov` overhead excluded); backends
reporting exact bytes (Rumble progressive `meta`/HEAD) show exact totals,
otherwise nominal upper bounds (VBR lands lower); JSON also
carries `video_size`/`audio_size` breakdown keys.

### Disk-space preflight and directories

`core/disk.py`, checked after quality selection (first point estimates
exist), skipped by `--no-space-check`, list modes return earlier:

- Workdir FS needs `estimate + 256MiB` (segments); output FS needs
  `estimate + 256MiB` (mux output). Same FS → ~2x + headroom total.
- Known-short fails naming need-vs-have plus remedies (`--no-space-check`,
  smaller `--quality`); unreadable (NAS/cloud drives) warns and proceeds.
- Mid-download write failures (ENOSPC with `--no-space-check`, permissions)
  fail cleanly via `io_fail()` naming the operation — single stderr line for
  future GUI bubbling; partial state stays resume-compatible.
- ffmpeg mux/remux failures are classified the same way (`_ffmpeg_error()`):
  explicit no-space text → disk-full message; write/trailer/closing EIO
  markers → disk-I/O message naming free space; anything else keeps the
  legacy generic message plus the raw tail.
- `--output-dir DIR` roots auto-named outputs (`-o` still wins outright);
  `--temp-dir DIR` roots `<stem>.ti22` workdirs (fast local disk, RAM
  drive, or NAS staging — created with `mkdir -p`).
- No dedicated `--use-ram-for-temp-dir` flag (parked by the numbers, not
  by taste): temp I/O is low single-digit percent of wall time on SSDs,
  `/dev/shm` is 50%-of-RAM (absent on macOS/Windows), and volatility plus
  Chromium shm contention outweigh seconds saved. Revisit only on measured
  evidence (temp I/O >10% of wall time on real hardware); `--temp-dir`
  already covers RAM-drive users explicitly.
- All user paths pass through `user_path()` (`~`/`$VAR` expansion — no-op
  when the shell already expanded, lifesaver otherwise).
- `-o` × `--output-dir` harmony: bare filename joins under the dir;
  dir-ful `-o` wins outright
  with an ignored-flag note, never silently. Resolved absolute output +
  temp paths print as notes on every run.
- `.env` loads after argparse, so `--help` never absorbs real env files
  (keeps the test suite hermetic against developer machines).
- Batch (shipped): resolve-all upfront gate on summed estimates (temp =
  `max` unless `--keep-intermediate`, else `sum`; output = `sum`, each +
  headroom), then per-video re-check on remaining estimates; sequential
  downloads with one shared session + persistent browser; download-N+1-
  while-muxing-N stays a fast-follow (see Open questions).

### Current module layout (`titanium_video_downloader/` package, v0.1.0)

Phase 1 package move done: verbatim code motion, prints intact (event bus
deferred), `ti22_video_dl.py` kept as a thin shim. 185 offline pytest tests green
(naming, selection, parsers, auth markers, stubbed login, login waiter,
env/no-auth/quality, batch ledger/gates, appconfig layering, per-site
cookies truth-check, vimeo, rumble incl. shorts + progressive-mp4,
fetcher incl. direct download, remux, ffmpeg resolve).

| Area | Module | Contents |
| ---- | ------ | -------- |
| Auth plumbing | `extractors/base.py` | `AuthError`, `fail`, `AuthProvider` ABC (`site_key`, `requires_auth`, `cookie_domains`), provider registry, host/hidden-input/challenge/marker helpers + redacted debug |
| StudyGateway | `extractors/studygateway.py` | `StudyGatewayAuth` (SAML + Playwright harvester with dual-signal post-submit wait — bad creds fail in ~10s naming the rejection — + embed resolve), `extract_ottdata`, `pick_playlist_url`, `resolve_config_url` |
| Vimeo public | `extractors/vimeo.py` | `VimeoAuth` (clip regex incl. unlisted/channels/groups; fast bare-config + watch-page Play-click intercept fallback (`--headed` forces upfront); `video.privacy` gate); downstream shared verbatim |
| Stubs | `extractors/rightnowmedia.py`, `generic.py` | Real `match()`, `NotImplementedError` elsewhere |
| Rumble public | `extractors/rumble.py` | `RumbleAuth` (watch→key→embedJS, muxed-HLS tar renditions + progressive-mp4 variant (all quality levels, Range-resume direct, empty audio), Shorts feed-JSON resolve (permalink-scoped, HEAD sizes), single-page browser bundle for gated origin fetches, live/DRM gates, shape-naming diagnostic on empty resolve); sizes nominal `meta` bytes (upper bound — VBR content lands lower); future: direct-ffmpeg HLS option for speed-over-progress users |
| Browser | `core/browser.py` | Process-shared headless Chromium singleton (launch-once, headed-mismatch relaunch, dead-browser detect, `atexit` + explicit close); per-call contexts preserve cookie isolation; segment threads never touch it |
| Batch | `core/batch.py` | `parse_batch_file` (`URL [QUALITY]`, fail-fast malformed), `dedupe_entries` ((url, effective-quality) keys), `EntrySkip`, URL heuristic |
| Session | `core/session.py` | UA/headers/timeouts, `new_session`, per-site `get_creds`, `load_cookies` (expiry warnings), per-site cookies env + jar domain truth-check |
| Env file | `core/envfile.py` | stdlib `.env` loader (flat `KEY=VALUE`), `titanium-software/ti22-video-dl` defaults |
| App config | `core/appconfig.py` | `config.toml` via stdlib tomllib (no new dep); CLI > env > file > builtins layering, lenient bad keys, `pick_always` tri-state with `--pick`/`--no-pick`, `prefer_cookies` gate for per-site cookies env, `--print-config` snapshot |
| Models | `core/models.py` | `parse_playlist`, select/labels, `resolve_segments`, `order_by_height`, `quality_items`, `pick_index` |
| Fetch | `core/fetcher.py` | `fetch_json`, `fetch_text`, `_get_with_retry`, `download_rendition` (threads, manifest resume; optional init/suffix/headers/assemble), `download_direct` (single-file Range resume, fail-closed size check), `fetch_hls_chunklist` (tar-wrapped or plain m3u8) |
| Disk | `core/disk.py` | `download_estimate` (single source with listings), split temp/output `check_space` gate + `human_bytes` |
| Mux | `core/mux.py` | `mux` (ffmpeg `-progress` parsing), `remux_concat` (TS→MP4; falls back to system ffmpeg — imageio 7.0.2-static (Linux) segfaults in mpegts demux on some files; Windows Gyan 7.1 build passed a live 15-seg tar remux clean; upstream report TODO) |
| Naming | `core/naming.py` | `sanitize`/`sanitize_path` |
| Paths | `core/paths.py` | `user_path()` (`~`/`$VAR`), `config_relative_path()` (relative names: CWD, then config dir), `ffmpeg_path()` resolution order |
| CLI | `cli.py` | argparse front-end + orchestration (`_resolve_entry`/`_download_entry` split, `run_batch` ledger, layered config resolution, automatic auth); entry points `ti22-video-dl` script + `__main__.py` |

Key behaviors: AAC preferred over Opus; CDN fallback (`akfire` →
`fastly_skyfire`); resume via `.ti22/` manifest (deletes on success unless
`--keep-intermediate`; flushed every 10 segments and on failure, so crashes
and ENOSPC leave recent progress); assembly skipped when the combined file
is already fresh (mtime + size vs parts — torn files rebuild); output refusal
if target exists (single runs fail, batch entries skip with a note);
portable filenames (Windows-reserved set, strictest OS).

## Module layout, v0.1.0 actual (was: "Phase 1 target" sketch)

```
titanium-video-downloader/          # distribution name (pip/website); import package below
  titanium_video_downloader/        # import package (hyphens are not importable,
                               # so the distribution/readable name and the
                               # import name differ by design)
    core/
      models.py      # rendition shapes: playlist parse, quality select/labels,
                     #   segment resolve, quality-item rows (no dataclasses —
                     #   plain dicts survived contact with three providers)
      session.py     # login-capable session: constants, per-site creds,
                     #   cookie jar + expiry warnings + domain truth-check
      fetcher.py     # concurrent segment downloader + resume manifest
                     #   + direct progressive download (site-agnostic)
      mux.py         # ffmpeg mux w/ progress parsing + binary resolution
      browser.py     # process-shared Chromium singleton (batch amortization)
      batch.py       # batch file parse/dedupe/ledger helpers
      naming.py      # sanitize/sanitize_path
      disk.py        # download estimates + split temp/output space gates
      paths.py       # user_path(), config-relative fallback, ffmpeg resolve
      envfile.py     # stdlib .env loader (+ which-file-loaded note)
      appconfig.py   # config.toml layering (CLI > env > file > builtins)
    extractors/
      base.py        # AuthProvider ABC: match/login/resolve_embed +
                     #   site_key/requires_auth/cookie_domains + registry
      studygateway.py# browser SAML harvest + tokenized embed resolve
      vimeo.py       # public Vimeo: config→playlist path, no-auth resolve
      rumble.py      # watch→key→embedJS (tar + progressive-mp4 + Shorts)
      rightnowmedia.py # stub — match real, rest waits for probe
      generic.py     # fallback slot for the unknown site
    cli.py           # argparse front-end + orchestration (resolve/download
                     #   split, run_batch ledger, automatic auth)
```

Still future: `core/events.py` (GUI prerequisite — today's stdout contract,
results-on-stdout/diagnostics-on-stderr, is the down payment),
`extractors/youtube.py` (deferred, deliberately), the PyInstaller binary
(parked — no binary built yet).


`ti22_video_dl.py` remains as a thin shim during transition, then retires.
Console-script / PyInstaller binary name: `ti22-video-dl` (`ti22-video-dl.exe`).

### Decisions (locked; v0.1.0 additions marked ★)

- **Login is part of the Extractor ABC**, not a studygateway special-case —
  both known sites need it, the unknown one does too.
- **No yt-dlp dependency** — hand-rolled extractors only (owner decision).
  The ABC is adapter-compatible if this is ever revisited, but no adapter
  will be built.
- **Single-file layout ends at Phase 1** — package route preferred by owner.
  (★ done: `titanium_video_downloader/` shipped, shim kept.)
- ★ **`best` = literal highest video quality** — no 1080p cap, no
  context-dependence; 2160p/4K downloads when published (pre-release, so
  no trained expectations to preserve).
- ★ **Login is automatic, never a user decision** — auth sites harvest,
  public sites skip; `--use-browser-login` removed as ceremonial once
  requests-SAML died (existing config keys degrade to a lenient note).
- ★ **Cookie files are debug/dev, default off** — explicit exclusive file
  or per-site env + `--prefer-cookies`; no cookies table in config.toml
  (duplicate data — `.env` already persists); no `--no-prefer-cookies`
  (off is config-`false`/CLI-absence).
- ★ **Leniency philosophy for user files** — unknown keys, bad types,
  malformed TOML, blank values: `note:` naming the key (never values),
  ignore and continue. Explicit blank CLI values fail like bad flags
  (exit 2). Same spirit in `.env` (bad lines named) and cookie loading
  (expired entries warn-but-load).
- ★ **Uncertainty resolves toward waiting** (login waiter) and toward the
  conservative direction (config `no_auth` beats config `prefer_cookies`;
  truth-check misses fall back to normal auth). Fast paths fire only on
  positive evidence.
- ★ **Parked with rationale, not forgotten**: RAM-temp (numbers, not taste);
  2FA-distinct login branch (out for now); direct-ffmpeg-HLS (speed option,
  progress cost); GPAC Plan B (trigger: Windows binary equally broken or
  upstream silence + second TS-class failure).

### Event system (GUI prerequisite — still future)

Every print/tqdm call becomes `emit(Event)` with numeric payloads:

| Event | Payload |
| ----- | ------- |
| `segment_progress` | `{stream, done, total, rate, eta}` |
| `mux_progress` | `{pct, eta}` / `{elapsed}` fallback |
| `info` | `{video, audio, quality, ...}` (today's info line) |
| `note` | `{message}` (e.g. filename sanitized) |
| `done` | `{path}` |
| `error` | `{message}` |

CLI subscribes and renders today's UI byte-for-byte. `--json` JSONL mode
falls out for free (one event per line on stdout) for out-of-process
frontends. GUI toolkit **undecided** — both in-process (import core) and
out-of-process (spawn + parse JSONL) stay open via this one mechanism.

## Multi-site plan

| Site | Status | Auth (`requires_auth` / `site_key`) | Notes |
| ---- | ------ | ---- | ----- |
| studygateway (VHX OTT) | ✅ working | `True` / `studygateway` | Browser SAML login + tokenized embed (done; bad-creds fail-fast waiter, v0.1.0 package) |
| Vimeo public | ✅ working E2E | `False` / `vimeo` | Fast bare-config for lax videos + headless watch-page Play-click intercept (player needs embedding context; bare player page idles) capturing minted `h=`/`s=`; `video.privacy` gate fails closed (private/password deferred); 576+577-seg 1080p+AAC download verified live |
| RightNow Media | stub only | `True` / `rightnowmedia` | Login-required; UNVERIFIED similarity-to-StudyGateway hypothesis; needs login-flow + DRM probe before implementation |
| Rumble public | ✅ working E2E | `False` / `rumble` | watch→key→embedJS (all endpoints 200 cookie-less in probes, but origin 403s bare `requests` from some egress — single-page browser bundle for origin fetches, CDN stays in `requests`); muxed-HLS renditions (tar-or-plain chunklists) + progressive-mp4 variant (all quality levels incl. 1440/2160, single Range-resume GET, no ffmpeg, audio inside); shorts (/shorts/<id>, no oembed key — page feed JSON scoped by permalink, HEAD sizes, same progressive tail); nominal `meta.size` bytes (upper bound); single shared AAC (tar path); live gate fails closed; 15-seg 1080p download + progressive-mp4 1080p download verified live; ads never fetched (inherent) |
| Unknown (login, ex-OBS) | slot only (`generic.py`) | `True` / `generic` | **Blocked on DRM probe first** — see Risks |
| YouTube | deferred (no module) | — | Hand-rolled player-response + signature cipher if revived; cipher changes = ongoing maintenance, isolated in one tested module |

### Blocked probe: unknown-site DRM (pending, unblocks the generic slot)

Play the unknown site's video with DevTools → Network open, look for:
`.../license` or `widevine` requests (DRM present?) and `pssh` in the
manifest/MSE traffic. Report back: DRM yes/no + login flow calls
(`session`/`auth`/`login`). No code until this answer exists.

## Packaging / distribution (non-tech users)

- **Tool: PyInstaller** (boring, documented, fits requests+subprocess shape).
  Nuitka is the revisit-if-it-hurts alternative. **Cython is not a packager**
  (needs an interpreter + deps) — no role here.
- **No cross-compiling**: each OS builds its own binary.
  - Windows `.exe` → native Windows Python on the host (NOT WSL2).
  - Linux binary → WSL2.
  - macOS → GitHub Actions `macos-latest` (no Mac owned; cloud Mac closes it).
- **Platform order: Windows first**, macOS second, Linux last
  (`pipx install` covers Linux users without any exe).
- **ffmpeg: bundled** via `imageio-ffmpeg==0.6.0` (ffmpeg 7.0.2-static,
  optional `ffmpeg` extra, installed by default in our envs/CI/bundles).
  Resolution order in `core/paths.py::ffmpeg_path()`: `--ffmpeg-path` >
  imageio-ffmpeg extra > system PATH. `-c copy` remuxes verified
  content-equivalent across system 6.1.1 and bundled 7.0.2 (stream specs
  identical, 1-byte container delta, null-decode clean).
- **ffmpeg alternatives, evaluated and parked (BillyBoy's choice c).**
  Neither Bento4 nor GPAC/MP4Box replaces ffmpeg today. Bento4 is out on
  three independent grounds: GPL-or-commercial licensing (worse than our
  current LGPL posture), dormant upstream (site © 2020, empty releases,
  568 open issues), and tool-shape mismatch (`mp4mux` wants elementary
  streams, not our fMP4 fragments / TS segments). GPAC is the named Plan B
  (LGPL-2.1 like today, active upstream, `m2tsdmx` is a *different* TS
  parser that may not share the segfault) — but unproven until spiked.
  Trigger for the spike: Windows binary equally broken at packaging
  acceptance, or upstream silence plus a second TS-class failure. Spike
  shape: CI-build minimal static MP4Box (`./configure --static-bin`,
  build tools + zlib only), run the crashing segments + a DASH set through
  it, byte-compare against ffmpeg outputs, assess progress-output rework
  (no `-progress pipe:1` equivalent — quiet remux has precedent). Bento4
  inspection tools (`mp4dump`/`mp4info`) remain fine as dev-only forensics;
  using the tools is not bundling them.
- **Signing: deferred.** Expect SmartScreen/Gatekeeper warnings until
  Windows cert + Apple Developer ID are wired into CI as secrets.
- **Installer offers the config dir.** The loaders only read — nothing
  creates `~/.config/titanium-software/ti22-video-dl/` (Linux/macOS,
  offer default-yes) or `%APPDATA%\titanium-software\ti22-video-dl\`
  (Windows, no choice offered — Program Files isn't user-writable, so
  per-user config is mandatory). Optionally seed commented templates
  (shaped like `.env.example`, values empty) for `.env` and `config.toml`.
- **CI matrix** (acceptance criterion of the first binary): one workflow ×
  `[windows, ubuntu, macos]` runners → native exe each → smoke-test the
  *bundle* (`--help` + offline parser tests) → attach to GitHub Release on
  tags. Local builds stay for fast iteration.
- **Phase 1 packaging checklist** (pending — no binary built yet): `titanium_video_downloader.cli:main` +
  `__main__.py` entry points, release binary `ti22-video-dl`; no hidden dynamic
  imports (declare `hiddenimports`); no `__file__`-relative paths
  (`importlib.resources` only); tiny dep surface (`requests` + stdlib +
  optional `tqdm`).

## Risks (priority order)

1. **Unknown site may be Widevine-DRM** — hard wall for hand-rolled
   download (needs a CDM), not an engineering task. The blocked DRM probe decides.
2. **YouTube cipher maintenance** — accepted cost of no-yt-dlp; contained
   in one module with its own tests.
3. **Scope creep on GUI** — deferred; the event bus is the only
   prerequisite (package move already done).
4. **Windows long paths** — deep workdirs + long titles can exceed 260
   chars; no extended-length handling until a real report. Cross-OS
   otherwise: `f_bavail` (Unix) with `shutil` fallback (Windows),
   portable errno set, forward-slash concat entries.

## Conventions

- 2-space indent, stdlib + `requests` (+ optional `tqdm`, `playwright`
  via `browser` extra, `imageio-ffmpeg` via `ffmpeg` extra).
- Verify offline first (stubbed network, generated ffmpeg fixtures);
  live runs need fresh pasted URLs (60s config window).
- Git tracking: package, tests, docs, and packaging files are tracked
  (see `.gitignore` allowlist); `.env`, media, fixtures, workdirs,
  `__pycache__`, and venvs stay ignored. Never commit secrets.
- This file is the decision log — update it when a status above changes.

## Open questions

- GUI toolkit (pending; event bus first).
- Batch pipeline parallelism (download N+1 while muxing N) — fast-follow;
  batch ships sequential with a persistent browser.
- Identical-name output overwrite reported once (file present at launch,
  no `--overwrite` flag exists yet) despite the `exists()` guard — could
  not reproduce from code audit; needs a targeted repro before any fix.
