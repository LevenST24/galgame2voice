# Changelog

All notable changes to galgame2voice are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- ADV interface (P0–P8): line-by-line playback with voice, acting tags,
  layered sprites with blink and lip sync, LOG history and voice replay,
  settings, backgrounds by time of day, and a title screen with session loading.
  ADV is now the only UI, with a live-rotating phone portrait layout, touch
  controls, sprite/title fades that respect reduced motion, and background and
  queued-face preloading. Portrait and background assets stay local only.
- Background packs per work (`backgrounds/<work>/manifest.json`). A character
  package can name its `work`; the background picker lists that work first, and
  its default "自动" shows the work's default background.
- `scripts/dev_standins.py` starts a fake LLM and a fake GPT-SoVITS server (old,
  per-line and deliberately broken reply formats; WAVs with a speech-like envelope)
  so the chat, voice and playback pipeline runs without a GPU or a paid model.
- `docs/design/adv-ui.md`, the design spec for the coming visual-novel interface.
- "允许私网 LLM 端点" switch in chat model settings for Ollama, LM Studio and LAN
  models. It saves on change; testing or enabling a model waits for the save.
  Model lists and connection tests now apply the same private-network rule as chat.
- A reply that never starts leaves a note under the user's message with the reason,
  a "重新发送" button that retries the same message in place (no duplicate bubble,
  recordings kept) and, for model problems, a shortcut to the model settings.
- Chat errors carry a stable `error_code` and readable Chinese guidance: a missing
  API key names the provider, and a blocked private endpoint points to the switch
  above instead of being reported as a DNS failure. Connection tests explain a
  missing key the same way.
- The header badge says "未填写 API Key" when the active cloud provider has no key,
  and "正在读取模型配置…" until the model list loads instead of briefly claiming that
  no model is configured.
- `scripts/verify_windows_portable.py` checks the release ZIP's checksum, required
  files, empty user folders, excluded secrets and models, and that its web files
  match the deployed and built frontend.

### Changed
- ADV: LOG no longer lists lines of the current reply that the stage has not
  shown yet, so opening it mid-reply does not reveal the next lines.
- The browser now asks for per-line replies with acting tags by default. Session
  settings → 回复格式 → "旧格式：整段回复" switches a session back. Sessions saved
  with the earlier default also move to the new format; API clients that omit
  `reply_format` still get the old format.
- Voice replies play by default in a browser that has no saved settings, so the
  first ADV conversation is voiced. A browser that already saved the voice switch
  keeps it; "语音开关" on the stage or 设置 → 自动播放语音 changes it.
- Sending a message goes through `frontend/src/conversation.js`, a DOM-free
  controller that emits turn, text, audio, done, error and cancelled events. The
  chat view subscribes to it now and the coming visual-novel stage will reuse it;
  nothing visible changes. `main.js` starts through an injectable `initApp()`, and
  the history and model-badge tests import the real modules instead of slicing
  source text.
- Persona prompts without the bilingual reply format (new custom voices, edited or
  third-party packages) get the standard JSON format appended, so replies keep a
  Japanese line for the voice instead of having Chinese text read aloud.
- Toasts stay above the message input as it grows, and only their action button
  takes clicks, so retyping after an error no longer opens settings by accident.
- Portable builds explain updating by extracting a new package instead of offering
  a git update.
- The Docker image starts through `scripts/run_container.py`, which turns console
  authentication on unless `GALGAME2VOICE_AUTH_DISABLED` is set. The default moved
  out of the image's `ENV`, where its name failed Docker's build checks.
- The LLM endpoint check now treats every non-global address as private, including
  `100.64.0.0/10` (shared CGNAT space, which Tailscale uses) and IPv4 addresses
  carried in IPv4-mapped, 6to4 and Teredo IPv6. A model served over Tailscale
  needs 「允许私网 LLM 端点」 turned on.

### Fixed
- Holding Ctrl to fast-forward until a reply ends in input mode no longer leaves
  a stale temporary-skip flag that later turns off a manually enabled `SKIP` when
  the window loses focus or when skip is toggled.
- `resolve_reference_audio_path()` and `validate_reference_audio()` now check
  `project_file()` before falling back to the current working directory for
  relative paths, preventing colliding files in the launching directory from
  overriding project reference audio.
- `find_ffmpeg()` now falls back to a bundled `imageio-ffmpeg` binary when no
  system `ffmpeg` is on `PATH`, so `doctor` and audio conversion work in virtual
  environments that have only the wheel installed.
- Cached TTS WAV files under `/audio/cache/` keep their `Cache-Control: public,
  max-age=31536000, immutable` header instead of having it overwritten with
  `private, max-age=0` by the general `/audio/` middleware rule; message deletion
  runs inside `immediate_transaction` for WAL savepoint safety.
- On the ADV stage, re-recording voice input revokes the previous recording's
  Blob URL and guards async `recorder.onstop` callbacks across resets; audio cache
  lookups refresh LRU order; suspended `AudioContext` instances resume on stage
  interaction or fall back without stalling AUTO mode; modal overlay clicks no
  longer bleed through to advance the stage; and SSE parsing handles `CRLF` line
  endings.
- After sending a message, Space, Enter and Esc work at once on the ADV stage.
  Sending hid the focused input box, focus fell to the page body, and the stage's
  shortcuts heard nothing until the stage was clicked; Esc could not stop a reply
  that was still generating.
- Two database backups taken within one tick of a coarse system clock (about 2 ms
  on some Windows machines) got the same name, so the second replaced the first.
  The second now waits for the next tick.
- Voiced sentences without their own emotion cue now show the reply's declared
  emotion on the portrait instead of always falling back to "gentle".
- Turning automatic voice playback off is saved immediately; a reload no longer
  turns it back on.
- Project-relative reference audio resolves against the install root, so a
  relocated data root or a launch from another working directory still sends an
  absolute path to GPT-SoVITS.
- Character rescans run one at a time; lookups keep the last complete catalog during
  a scan, and a failed scan leaves it intact. Rescanning keeps an unsaved persona
  draft and the selected model files.
- Loading older history waits for the current reply, and sending waits for the older
  history to finish loading.
- The Docker image creates the characters folder for its non-root user and compose
  mounts `./characters`; the app also starts when that folder cannot be created.
  The build context now contains only what the image copies, so character models
  and user data are no longer uploaded on every `docker build`.
- `doctor` probes GPT-SoVITS at `/control`, like the app's own health check. It
  no longer reports a running api_v2 server as unreachable.
- The one-click update no longer runs a local frontend build when the update
  already includes the built frontend. Installs without Node.js no longer see a
  build failure after a successful update.
- Long-term memory no longer mistakes everyday phrases such as "我叫了个外卖",
  "别叫我笨蛋" or "明天叫我起床" for the player's name. Before, the character
  started calling the player by that phrase, and the web UI cannot correct it.
- A new "我喜欢…" or "我讨厌…" about the same thing replaces the opposite memory
  instead of keeping both. The player's name and occupation are still recalled
  after more than 200 newer memories, and memory recency is computed in UTC.
- The relationship stage sent to the model now changes at the same scores as the
  level (20 and 80), so Lv.5 no longer comes with the third stage's guidance.
  Level-ups are judged from the score alone.
- The memory API answers 422 instead of 500 for a value that is empty after
  sanitizing, and no longer ignores such a value on update.
- Global settings can no longer be saved before they have loaded, and editing a
  field while they load no longer leaves the others at their defaults. Either
  case used to overwrite the saved settings, turning the Telegram bot off and
  resetting the engine address and precision. Restarting the engine before the
  settings load keeps its saved precision.
- When the voice list fails to load, saving session settings keeps the session's
  character instead of unbinding it. Deleting a voice, or finding a session's
  voice deleted, also clears the binding on the server, so it no longer returns
  after a reload.
- Characters created without a persona can save their session settings again.
  Saving session settings or deleting another voice no longer reloads the
  portrait, which reset its costume and last line.
- Clicking the "聊天记录备份与恢复" heading no longer opens the import file picker.
- After `/character`, Telegram replies use the new character's persona, memories
  and affection instead of those of the character the chat started with.
- In Telegram groups, `/nickname` is saved for the member who sent it, and one
  member's message, `/reset` or clear button no longer cancels another member's
  pending voice note.
- Telegram voice notes sent while the active provider has no speech recognition
  get a clear reply instead of a generic error, and a configured speech
  recognition model is used.
- Restarting the Telegram bot while it is still starting (for example by saving
  settings right after launch) no longer leaves a second bot polling.
- One slow model reply no longer holds up the Telegram bot for every other chat;
  each member's messages are still answered in order.
- A reply that does not follow the JSON format is shown, saved and voiced in full;
  before, only its first 16 or so characters remained. A short preamble before the
  JSON ("好的～") no longer puts raw JSON in the chat bubble instead of the reply,
  and the "日文" label no longer shows up at the end of a labeled reply.
- The chat bubble and its local history end with the reply as the server saved it.
- Stopping a reply, or a provider error during one, saves the partial reply once
  instead of twice; the copy was repeated in the next turn's context. Pressing Stop
  or closing the tab while a reply streams now keeps the partial reply, and removes
  a message that got no reply, instead of losing both with the cancelled request.
- An empty reply no longer earns affection. It withdraws the player's message, so
  sending the message again counted twice. The non-streaming chat API now awards
  affection only after the reply is saved.
- A reply with an empty "chinese" field is no longer shown, saved and voiced as
  raw JSON in the non-streaming API and Telegram; Telegram reports it as a failed
  reply.

### Removed
- The speech recognition engine selector (本地 SenseVoice / Whisper) in global
  settings. Those engines were never connected; voice input always used the
  browser's speech recognition.

### Security
- The Telegram bot applies its admin whitelist to every command and button, not
  only to chat messages. Before, anyone who found the bot could switch the active
  character (also for the web UI) and view the model settings and spending.
  Rejected users are told their Telegram user ID so the owner can whitelist it.

## [2.1.0] - 2026-10-14

### Added
- Hot-reload character package discovery endpoint (`POST /api/characters/rescan`)
  and a "扫描" button in session voice settings, allowing newly extracted
  character packages in `characters/` to be discovered and bound immediately
  without restarting the server.
- Cursor-based session history pagination (`GET /api/chat/history?before_id=...`
  with `has_more` and `oldest_id`), and an in-chat "↑ 查看更早的聊天记录" action
  that prepends older messages while preserving scroll position.
- Actionable recovery buttons on error/info toasts (one-click navigation to
  LLM model configuration, voice engine status, session voice settings, and
  chat backup settings), plus first-install setup chips in empty chat sessions
  and clickable header/diagnostic status badges that also show the session's
  bound character name.
- Full chat export (`GET /api/chat/export`) that reads one database snapshot and
  includes sessions and messages outside the sidebar/history limits. Browser-only
  text is retained separately when it differs; repeated messages remain distinct.
  Offline export is clearly labeled.
- Atomic chat import (`POST /api/chat/import`) preserving original session and
  message timestamps; oversized backups fail explicitly instead of truncating.
- Portrait layer exporter (`scripts/tools/compose_portrait_layers.py`) that
  preserves original per-layer coordinates and ordering in one shared costume
  frame, validates source resolution, and records source hashes.
- Verified SQLite snapshots including committed WAL data, daily startup backups,
  and a portable database recovery launcher (`--restore-database` / `数据恢复.bat`)
  that preserves original files.
- Actionable runtime recovery guidance, bounded settings requests, and engine
  readiness polling that distinguishes model loading from startup failure.
- Repeated portable launches reuse the running installation and its actual port.
- Local GPT-SoVITS directory selection and persistence in the settings console;
  sibling engine paths survive relocation and feed model scans and restarts.
- Portable startup exceptions leave a diagnostic log in `logs/launcher_error.log`.
- Windows x64 portable packaging with a frozen interpreter, locked application
  dependencies, frontend, FFmpeg and included third-party license files.
- `--no-engine` opens the application without managing a local voice engine.

### Changed
- Chat titles and model names use separate lines; message spacing, contrast,
  controls and portrait panels remain readable in narrow windows.
- Global settings use category navigation and concise descriptions. Session
  settings keep character controls visible and group advanced tuning in details.
- Dialogs preserve focus, support keyboard navigation and handle nested token
  prompts. Closed drawers and portraits are excluded from keyboard navigation.
- Portrait image URLs include file revisions so re-exported images invalidate
  browser and preload caches even when their names are unchanged.
- Developer documentation is grouped under `docs/development/`; local caches and
  archived scratch files stay under `.local/`, excluded from source releases,
  Docker contexts and model discovery. Added `使用说明.md` folder guide.

### Fixed
- Release packaging (`scripts/package_release.py`) now excludes archive files
  (`.zip`, `.7z`, `.rar`, `.tar`, `.gz`, `.xz`) and additional weight formats
  (`.pt`, `.gguf`) so user-downloaded character archives in `characters/` never
  bloat source release bundles.
- Session persona editor is disabled when no character is bound (except during
  new custom voice creation, where the typed persona is automatically saved with
  the new profile), preventing accidental unsaved edits.
- Session lists paginate beyond the first 50 entries. Saves for one session run
  in order; deletion waits for them and removes local records only after confirmation.
- Deleting or clearing chats leaves shared audio to the normal retention worker,
  protecting other conversations, reference files, and audio during transaction rollback.
- External data directories no longer leak absolute host paths in system status.
  History timestamps interpret SQLite dates as UTC, and failed history loads can be retried.
- Streaming text renders each increment immediately. The final message takes
  over live playback controls without restarting completed speech, losing
  pause state, or advancing portrait dialogue ahead of paused audio.
- Audio downloads, body reads, decoding and cache access have bounded waits.
  Failed chunks release the queue, surface a recoverable message and allow later
  speech to continue. Playback itself fills the cache without a duplicate fetch.
- Repeated sentences sharing a cache URL retain their separate playback slots.
  Pausing stays effective as new chunks arrive, and portrait cues use the audio
  clock rather than wall-clock timers. Blocked autoplay offers a click to resume.
- Late cache reads, translations and synthesis results cannot steal playback
  from a newer click or session. Clicking a loading message again cancels it;
  temporary connection failures preserve its audio references instead of
  triggering unnecessary synthesis. Only confirmed missing audio is regenerated.
- Configuration errors no longer replace diagnostic widgets. Refreshing after
  a failed request restores the status display and preserves unsaved fields.
- Explicitly clearing a session voice binding now removes the SQLite binding;
  omitted bindings retain their existing value, and empty settings can be saved.
- Voice creation binds to its originating session, stale selector responses cannot
  replace a newer session's options, and the active voice remains selectable.
- Voice, persona, Telegram, cache and session requests have deadlines without
  automatic write retries. Failed persona saves preserve the open editor and its
  text; failed session saves no longer claim confirmed persistence.
- Audio cleanup defers when reference or cache protection metadata is unavailable,
  preserves cache entries whose files cannot be safely removed, and moves expired
  cache disk operations off the event loop. Registered reference samples inside
  the cache also remain protected. Cleanup resumes after recovery.
- Late history loads cannot replace newly typed messages or revive a deleted
  session, and session switching renders immediately while history loads.
- Developer documentation is grouped under `docs/development`; local caches and
  archived scratch files stay under `.local`, excluded from source releases,
  Docker contexts and model discovery. A short Chinese folder guide identifies
  the launch entry point, user data and development files.
- Restored schema version 7 compatibility after an upstream sync dropped the
  legacy foreign-key migration. Existing databases keep their version and data;
  older character cascades are rebuilt transactionally without losing indexes,
  triggers or sequence counters, and unknown future schemas remain blocked.
- Portraits support existing emotion-named PNG packs as well as numbered
  differential indexes, without rewriting installed character assets.
- The portrait stage follows the current session even when voice switching
  fails or the engine is offline. Stale metadata, images and stream completions
  cannot replace the newly selected character; costume fallbacks stay in the
  selected outfit, and unavailable portraits offer explicit recovery guidance.
- Dialogue cards receive reply text instead of message objects. Frontend HTML
  uses LF consistently so Windows builds match the committed web artifacts.
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
