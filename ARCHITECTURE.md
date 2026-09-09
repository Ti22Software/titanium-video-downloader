# Titanium Downloader — Architecture (living doc)

Single-file-first streaming video downloader, growing into a multi-site,
GUI-capable app with downloadable executables for non-tech users.
Release binary name: `ti22-dl` (`ti22-dl.exe` on Windows).

## Current state (verified working)

`ti22_dl.py` downloads one studygateway video end-to-end: watch slug, embed,
or config URL in, muxed 1080p MP4 out. Verified against a live ~800MB download
(ffprobe specs, null-decode, VLC playback, md5-determinism across runs).
Happy-path SAML plumbing verified live (302 → SAMLRequest → POST → browse),
but pure-requests login does NOT authenticate: server demands reCAPTCHA v3
(empty g-recaptcha-response rejected) and /browse is PUBLIC (200 logged-out),
so the old status-based verify false-positived `login ok`. Verify is now
authoritative via `_current_user`/logout markers; unauthenticated browse
correctly raises `login rejected`. Primary login path is now
`--use-browser-login` (Playwright Chromium harvests real session into the
same pipeline); `--cookies` remains the manual fallback.

### Pipeline: the 4 hops

```
watch.studygateway.com/.../videos/<slug>  (auto-login: --email/--password
  or TI22_EMAIL/TI22_PASSWORD; --cookies Netscape fallback; --login-only test)
  → GET watch/login (302 Location: www.../login?SAMLRequest=...)
  → GET www/login?SAMLRequest=... (_token + hidden SAMLRequest/RelayState)
  → POST www/login/saml (login-type=original; requires real reCAPTCHA v3
    token — requests-only posts stay logged-out; use --use-browser-login)
  → verify AUTHORITATIVELY via _current_user/logout markers (browse is
    public 200 either way; cookie presence proves nothing)
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

### Token lifetimes (drive ordering + error handling)

| Token             | Lifetime | Failure mode                    |
| ----------------- | -------- | ------------------------------- |
| Embed auth-token  | hours    | Embed page 401/403 → re-paste   |
| Config token      | ~60s     | Config fetch 410 → "token expired" |
| Playlist URL      | ~5380s   | Segment 401/403/410 → re-resolve |

Pasted config URLs are inherently fragile (browser may have consumed the
single-use token; 60s expires while copy-pasting). The reliable inputs are the
**watch slug URL** (browser login mints its own embed/config tokens in
seconds) or the **embed URL** — both minted-and-consumed live.
Login paths: `--use-browser-login` (Playwright Chromium, passes reCAPTCHA v3
natively; needs `pip install playwright && playwright install chromium`),
`--cookies` Netscape export (manual fallback). Pure-requests password login
is kept for plumbing/tests but rejected live by reCAPTCHA — no solver.
See Phase 3.

### Current module layout (`ti22_dl.py`, ~500 lines)

| Area | Functions |
| ---- | --------- |
| Resolve | `resolve_config_url`, `extract_ottdata`, `fetch_text` |
| Config/playlist | `fetch_json`, `pick_playlist_url`, `parse_playlist` |
| Selection | `select_video`, `select_audio`, `label_for`, `audio_label` |
| Fetch | `_get_with_retry`, `download_rendition` (threads, manifest resume) |
| Mux | `mux` (ffmpeg `-progress` parsing) |
| Output | same-line `\r` progress + ETA (stdlib fallback; tqdm branch kept), `sanitize`/`sanitize_path` |

Key behaviors: AAC preferred over Opus; CDN fallback (`akfire` →
`fastly_skyfire`); resume via `.ti22/` manifest (deletes on success unless
`--keep-intermediate`); output refusal if target exists; portable filenames
(Windows-reserved set, strictest OS).

## Target architecture (Phase 1: package + events)

```
titanium-downloader/          # distribution name (pip/website); import package below
  titanium_downloader/        # import package (hyphens are not importable,
                              # so the distribution/readable name and the
                              # import name differ by design)
    core/
      models.py      # VideoInfo, Rendition(A/V), DownloadResult — dataclasses
      session.py     # login-capable session: cookies, token refresh, expiry errors
      fetcher.py     # concurrent segment downloader + resume manifest (site-agnostic)
      mux.py         # ffmpeg mux w/ progress parsing + binary resolution
      events.py      # event bus — replaces ALL prints (see below)
      naming.py      # sanitize/sanitize_path
    extractors/
      base.py        # Extractor ABC: match(url) / login(creds) / resolve(url)->VideoInfo
      studygateway.py# current pipeline, moved verbatim
      vimeo.py       # public Vimeo: config→playlist path, no-auth resolve
      youtube.py     # player-response parsing + signature cipher (hand-rolled)
      generic.py     # HLS/DASH-from-page-HTML fallback slot for the unknown site
    cli.py           # argparse front-end (current flags), renders events as today's UI
```

`ti22_dl.py` remains as a thin shim during transition, then retires.
Console-script / PyInstaller binary name: `ti22-dl` (`ti22-dl.exe`).

### Decisions (locked)

- **Login is part of the Extractor ABC**, not a studygateway special-case —
  both known sites need it, the unknown one does too.
- **No yt-dlp dependency** — hand-rolled extractors only (owner decision).
  The ABC is adapter-compatible if this is ever revisited, but no adapter
  will be built.
- **Single-file layout ends at Phase 1** — package route preferred by owner.

### Event system (GUI prerequisite)

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

| Site | Status | Notes |
| ---- | ------ | ----- |
| studygateway (VHX OTT) | ✅ working | Needs Phase 3 login automation (hop 1) |
| Vimeo public | planned (Phase 2a) | Reuse config→playlist path; no-auth resolve from public player page |
| YouTube | planned (Phase 2b) | Hand-rolled player-response + signature cipher; cipher changes = ongoing maintenance, isolated in one tested module |
| Unknown (login, ex-OBS) | slot only (Phase 3) | **Blocked on DRM probe first** — see Risks |

### Phase 0 probe (owner, ~30 min, unblocks Phase 3)

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
- **ffmpeg: bundle it** (version control + zero setup). Lean:
  `imageio-ffmpeg` (versioned static builds per OS) with system-ffmpeg
  fallback and a `--ffmpeg-path` override. Mux module resolves the binary
  at runtime instead of assuming PATH.
- **Signing: deferred.** Expect SmartScreen/Gatekeeper warnings until
  Windows cert + Apple Developer ID are wired into CI as secrets.
- **CI matrix** (acceptance criterion of Phase 1): one workflow ×
  `[windows, ubuntu, macos]` runners → native exe each → smoke-test the
  *bundle* (`--help` + offline parser tests) → attach to GitHub Release on
  tags. Local builds stay for fast iteration.
- **Phase 1 packaging checklist**: `titanium_downloader.cli:main` +
  `__main__.py` entry points, release binary `ti22-dl`; no hidden dynamic
  imports (declare `hiddenimports`); no `__file__`-relative paths
  (`importlib.resources` only); tiny dep surface (`requests` + stdlib +
  optional `tqdm`).

## Risks (priority order)

1. **Unknown site may be Widevine-DRM** — hard wall for hand-rolled
   download (needs a CDM), not an engineering task. Phase 0 probe decides.
2. **YouTube cipher maintenance** — accepted cost of no-yt-dlp; contained
   in one module with its own tests.
3. **Scope creep on GUI** — deferred; Phase 1 event bus is the only
   prerequisite.

## Conventions

- 2-space indent, stdlib + `requests` (+ optional `tqdm`) only.
- Verify offline first (stubbed network, generated ffmpeg fixtures);
  live runs need fresh pasted URLs (60s config window).
- Git allowlist: only `ti22_dl.py`, `ARCHITECTURE.md`
  (+ packaging files as added) are tracked; `.env`, media, fixtures,
  workdirs stay ignored. Never commit secrets.
- This file is the decision log — update it when a status above changes.

## Open questions

- GUI toolkit (after Phase 1).
- Bulk/series downloads (deferred; single-file only for now).
