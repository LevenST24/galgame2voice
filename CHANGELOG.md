# Changelog

All notable changes to galgame2voice are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Verified SQLite snapshots including committed WAL data, daily startup backups,
  and a portable database recovery launcher that preserves original files.
- Chat backup export and atomic history import, with protection for corrupt,
  unavailable, full, or concurrently modified browser storage.
- Actionable runtime recovery guidance, bounded settings requests, and engine
  readiness polling that distinguishes model loading from startup failure.
- Repeated portable launches reuse the running installation and its actual port.
- Local GPT-SoVITS directory selection and persistence in the settings console;
  sibling engine paths survive relocation and feed model scans and restarts.
- Portable startup exceptions leave a diagnostic log in `logs/launcher_error.log`.
- Windows x64 portable packaging with a frozen interpreter, locked application
  dependencies, frontend, FFmpeg and included third-party license files. The
  release workflow builds and tests the extracted ZIP with a restricted PATH.
- Portable user data stays beside the executable, outside `_internal`.
  Frozen launchers never run themselves as pip or as an engine's Python.
- `--no-engine` opens the application without managing a local voice engine.

### Fixed
- Interrupted chat streams preserve partial replies and report incomplete
  responses; cancellation releases readers and does not resend generation.
- Voice switching respects memory protection instead of automatically forcing
  a second attempt after a 503; failed saves and refreshes preserve input.
- Engine restarts only stop owned processes, serialize operations through browser
  disconnects, and validate the installation before changing configuration.
- Windows job APIs preserve 64-bit handles; shutdown closes the job and only
  terminates processes owned by the launcher, without trusting stale PID files.
- Native file pickers serialize Tk creation and cleanup on their owning thread.
- Saving an engine directory invalidates model discovery without allowing an
  older in-flight scan to repopulate the cache; pending settings reads preserve edits.
- Credential encryption now declares a locked `cryptography` dependency and
  emits only AES-GCM/DPAPI ciphertext. Legacy stream ciphertext is read only for
  migration; unreadable, damaged or unpersisted master keys fail explicitly.
- Docker uses the Settings-based entry point with authentication enabled by
  default, so its listener and network-exposure check agree.
- Fresh databases start without machine-specific voice profiles. Session voice
  settings guide first-time setup, while existing profiles are retained.
- Dynamic CRUD updates validate explicit SQL column allowlists. Directory
  browsing stays within authorized roots, and active-profile persistence errors
  reach callers instead of reporting a successful switch.
- Tests isolate credential, database, audio, log and temporary files and reset
  runtime singletons. Audio tests configure their own neutral voices; native-picker
  tests skip when tkinter is unavailable. CI compares deployed frontend artifacts
  with tracked files; blocking file operations are offloaded and ASYNC240 is enabled.
- TTS cancellation now stops stalled synthesis jobs without stopping the priority
  worker, and reaps enqueue helper tasks when audio buffers are full.
- Scheduler shutdown cancels queued jobs, wakes stream consumers, and drains the
  queue before the shared GPT-SoVITS HTTP pool closes.
- Cancelled upstream streams no longer block while writing errors or EOF into an
  abandoned full buffer; service streams close their nested generators explicitly.
- Interrupted audio is excluded from the persistent cache. Single-flight and
  stream errors still reach callers without deferred unhandled-future reports.
- Queued bytes/file synthesis requests recheck the shared cache before inference;
  a regression burst of 16 identical requests now invokes the mock engine once
  instead of 16 times, including mixed bytes/file callers.
- Cache keys resolve speed/temperature aliases and quality presets using the
  engine's option parser, and include explicitly requested voice profile IDs.
  Versioned keys prevent reusing ambiguous older entries while retaining their
  audio files for conversation-history playback.
- Streams with caching disabled no longer retain every received audio chunk.
- Lifespan cleanup also runs when the serving context raises. Startup tests use
  isolated settings and mocked voice clients, and batch entry-point tests execute
  a stub launcher to check paths and arguments containing spaces and `!`.
- Non-repository version tests bound Git's parent-directory search, allowing
  temporary test directories located inside a development checkout.
- Cached file responses now read only file metadata; cached streaming starts
  with a bounded chunk rather than loading the whole clip and records one hit
  per playback. Closing the service stream immediately closes its disk reader.
- Audio clips exceeding the RAM budget remain on disk without displacing small
  hot entries. Disk streams also stop buffering if a file grows past the budget.
- File cache lookups detect deleted or empty files even with a warm RAM entry.
  Concurrent overwrites read previous sizes after write admission, keeping disk
  capacity counters consistent and avoiding premature pruning.

## [2.0.0] - 2026-10-06

First tagged release. The single source of truth for the version is
`pyproject.toml` (build) / `galgame2voice.__version__` (runtime).

### Added
- `CHANGELOG.md`: release notes are now maintained per release.
- GitHub Release workflow (`.github/workflows/release.yml`): pushing a `v*`
  tag runs lint, the full test suite, rebuilds the frontend bundle, builds the
  release ZIP via `scripts/package_release.py`, and uploads it as a release
  artifact. The workflow also guards against new hardcoded `127.0.0.1:9880`
  literals outside the unified endpoint resolver.
- `galgame2voice/services/sovits_endpoint.py`: single unified resolver for the
  GPT-SoVITS engine address. Priority is now explicit and shared by every
  component: process environment
  (`GALGAME2VOICE_GPT_SOVITS_BASE_URL` / `GPT_SOVITS_BASE_URL`) > SQLite
  `gpt_sovits_url` (Web console) > `.env` > built-in default
  `http://127.0.0.1:9880`. Only loopback endpoints are managed as local
  processes; remote endpoints are never started, killed, or restarted by this
  host (`POST /api/system/restart_sovits` answers 409
  `REMOTE_SOVITS_NOT_MANAGED` for remote endpoints).
- Release packaging (`scripts/package_release.py`) now excludes credential and
  backup material from release ZIPs: `data/.master_key`, `data/.console_token`
  and the whole `data/backups/` tree are never shipped.

### Changed
- Version metadata unified at 2.0.0: `frontend/package.json` aligned from
  `0.1.0` to `2.0.0`; `docker-compose.yml` image tag pinned from `:latest` to
  `:2.0.0`; `scripts/package_release.py` reads the version from
  `pyproject.toml` instead of hardcoding it; `galgame2voice/config.py`
  `app_version` and the health-endpoint response schemas now default to
  `galgame2voice.__version__` instead of repeating the literal.
- `POST /api/system/restart_sovits` now restarts the *configured* endpoint
  (host/port from the unified resolver) instead of always spawning
  `127.0.0.1:9880`; it waits for the old process's port to be released
  (bounded polling, 409 `SOVITS_PORT_STILL_IN_USE` on timeout) instead of a
  fixed 1-second sleep.
- Launcher (`scripts/run_server.py`) no longer falls back to a local
  `127.0.0.1:9880` when the configured endpoint is unreachable, and no longer
  mistakes an unrelated listener on local 9880 for the configured engine. A
  remote endpoint that is down produces a clear warning instead of silently
  starting a local engine the app would never use.
- Saving `gpt_sovits_url` in the Web console re-resolves the effective
  endpoint before hot-applying it, so an explicit process environment variable
  keeps precedence instead of being silently overridden by the just-saved DB
  value.
- Rebuilt the committed frontend bundle (`galgame2voice/static/`) from
  `frontend/` via `npm run deploy`; the bundle is a build artifact again
  instead of a stale copy.

### Fixed
- Dashboard cache: the frontend reads `total_entries` but the backend only
  exposed `total_files`; the backend now returns the compatible field.
- `ChatService.stream_chat_events` now forwards `voice_profile_id` to
  `stream_chat` instead of dropping it.
- Empty/missing admin allowlist in `check_is_admin` is now fail-closed
  (consistent with `_is_admin`) instead of fail-open.
- FP16→FP32 auto-restart no longer drops the `device=` argument when
  respawning the GPT-SoVITS process.
- Release ZIPs no longer ship `data/.master_key`, `data/.console_token`, or
  `data/backups/` contents.
- ~170 code-health polish rounds corrected factually inaccurate prose
  (comments, docstrings, docs): false performance/efficiency claims, stale
  `(PLANNED)` markers for shipped features, absolute robustness/completeness
  claims, and inaccurate timeout/threshold values, among others. No behavior,
  API, or control flow was changed by these rounds.

### Known issues
- The committed `galgame2voice/static/` bundle is regenerated with
  `npm run deploy`; do not hand-edit it.
- 44 pre-existing test failures in the Linux environment (missing character
  assets, no network/GPU) are unrelated to this release; the failure set is
  byte-identical before and after these changes.

[2.0.0]: https://github.com/LevenST24/galgame2voice/releases/tag/v2.0.0
