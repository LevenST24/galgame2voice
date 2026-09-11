# Project: Galgame2Voice Hardening & Industrial Resilience

## Architecture
Galgame2Voice is an industrial-grade local AI Galgame companion and TTS voice streaming server.
- **FastAPI Core & Routers (`galgame2voice/routers/`)**: REST APIs and SSE streaming endpoints for chat, configuration, character management, system health, and audio serving.
- **Chat & Streaming Pipeline (`galgame2voice/services/chat_service.py`)**: `StreamingBilingualParser` for real-time Chinese token extraction and lookahead Japanese sentence segmentation; pipelined producer-consumer queue architecture with bounded buffers and coroutine lifecycle management.
- **Two-Tier TTS Cache & Synthesis (`galgame2voice/services/tts_cache_manager.py`, `gpt_sovits_client.py`)**: Tier 1 in-memory LRU cache (`OrderedDict`) + Tier 2 atomic disk WAV cache + SQLite index; thread-safe inference lock with exponential backoff retry.
- **Database Engine (`galgame2voice/database/`)**: SQLite WAL mode (`PRAGMA journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`) with `aiosqlite` and `BEGIN IMMEDIATE` transaction isolation.
- **Telegram Bot (`galgame2voice/telegram_bot/`)**: Async polling bot with multi-user isolation, task interruption, safe entity formatting, and hot-reloadable configuration.
- **Process & Lifecycle Management (`scripts/run_server.py`)**: Windows Job Object kernel binding (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) ensuring 100% VRAM, port (8080/9880), and child process reclamation on termination.

---

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Logging MaskingFormatter & Traceback Sanitization | Mask stack traces, unquoted tokens/keys, Google/Gemini keys, HuggingFace tokens in log records | M1_SECURITY | Survey R2 (DONE) |
| 2 | Error Detail & Token Sanitization in Responses | Sanitize raw exception strings in HTTP error JSON, SSE error events, `/api/telegram/test`, and Telegram bot chat replies to prevent token/URL disclosure | M1_SECURITY | Survey R2 (DONE) |
| 3 | Health API Path Normalization | Normalize `DatabaseTelemetry.path` to relative path `data/galgame2voice.db` in `GET /api/system/status` | M1_SECURITY | Survey R2 (DONE) |
| 4 | Memory Prompt Injection Defense | Strip newlines, delimiters (`【`, `】`), and injection keywords in `extract_facts_heuristic`; defensively wrap facts in `format_memory_prompt_block` | M1_SECURITY | Survey R2 (DONE) |
| 5 | TTS Cache In-Memory Microsecond Reordering | Reorder `TtsCacheManager.get()` to check in-memory LRU before disk stat syscalls to achieve `< 0.005ms` lookup latency | M2_PERF_STABILITY | Survey R3 |
| 6 | Database WAL Checkpoint & Concurrency Hardening | Ensure graceful WAL checkpoint on shutdown, verified atomic read-write concurrency under burst load | M2_PERF_STABILITY | Survey R1 |
| 7 | Full Suite Regression & Reliability Calibration | Calibrate test probe timeouts (e.g. in `test_empirical_challenger.py`) to guarantee 100% green across all 657+ tests under heavy system load | M2_PERF_STABILITY | Survey R4 |
| 8 | Opaque-Box E2E Test Suite (Tiers 1-4) | Systematic 4-tier test suite covering feature, boundary, combinatorial, and real-world workloads | M0_E2E_TESTS | Survey R4 (DONE) |
| 10 | Comprehensive Optimization Pass | Fix silent Telegram memory/affection integration bug (wrong method names), remove TTS cache lock held across prune I/O, bound the touch-throttle map, redact Telegram token prefix in logs, drop redundant SELECT in `add_message` via `RETURNING`, single-statement `upsert_memory` via `RETURNING`, reuse the open DB connection for history building (`get_history`/`build_chat_messages` accept `conn`), modernize deprecated `HTTP_422_UNPROCESSABLE_ENTITY` constants, calibrate Windows port-rebind test with bounded retry window | M3_FINAL_VERIFICATION | Optimization pass (DONE) |
| 11 | Industrial Self-Critique & Resilience Optimization | Lifespan graceful drain (`chat_service.aclose()` & `tts_cache_manager.aclose()` before WAL truncation checkpoint), `SessionManager` table name catalog caching (`self._table_name`) eliminating redundant `sqlite_master` per-turn queries, Voice model switch memory check calibration (1.5GB threshold + `force` override), and defensive fallback on SQLite `RETURNING` clauses | M3_FINAL_VERIFICATION | Self-critique pass (DONE) |
| 12 | Forensic Auditor & Skeptical Review Hardening | Resolved `SessionManager` query regeneration bug on stale cache retry, fixed `row_factory` safety for arbitrary connections, fixed voice switch REST 404 precedence over 503 pre-checks, implemented cross-platform `psutil` free memory detection with POSIX/Win fallbacks, replaced permanent LRU cache in `url_guard` with thread-safe TTL cache + `clear_dns_cache()` to close DNS rebinding SSRF window, made lifespan drain all active chat service instances, and added `test_m3_resilience_regression.py` | M3_FINAL_VERIFICATION | Auditor review pass (DONE) |
| 13 | Container & Multi-Platform Process Resilience | Linux container cgroups v1 & v2 slice hierarchy detection (`/proc/self/cgroup`), POSIX process signal lifecycle (`SIGTERM`, `SIGHUP`, `SIGQUIT`), cross-platform process tree termination fallback (`taskkill` / `pkill` / `os.killpg`) with PID 0/1/self safety bounds, Telegram bot `switch_active_profile` dispatch fix, voice switch reference audio change detection & TOCTOU deletion under lock | M3_FINAL_VERIFICATION | Multi-platform resilience pass (DONE) |
| 14 | Deep Transaction Atomicity & SSE Pipeline Hardening | Implemented SQL `SAVEPOINT` nesting and depth tracking in `immediate_transaction` guaranteeing outer rollback atomicity across all 24 CRUD writes, pruned empty/whitespace turns on LLM disconnect, added orphan cleanup in `chat_sync`, resolved `TtsCacheManager` dictionary race condition during burst touch throttling, closed generator sockets via `stream_gen.aclose()`, surfaced Anthropic SSE error chunks | M3_FINAL_VERIFICATION | Deep architectural audit pass (DONE) |
| 15 | Subsystem Router & RAG Boundary Hardening | Voice profile boundary constraints & length bounds, memory router character_id validation & fact sanitization, Characters router (`/api/characters`) REST endpoints with 404 precedence, health probe URL scheme validation & error sanitization, Telegram bot network drop resilience & nickname sanitization, Memory RAG empty stem matching fix & extreme length clamping | M3_FINAL_VERIFICATION | Subsystem audit pass (DONE) |
| 16 | Audio Converter, Affection Progression & Cache Hygiene Hardening | Affection multi-level jump milestone voiceline unlocks, new-user dialogue unlocks auto-initialization in SQLite, TTS cache put failure memory discard & orphaned file unlinking, burst-write multi-batch cache pruning to 80% capacity watermark, audio converter bounded process wait timeout, fast non-audio magic header check rejection, metrics collector defensive input type guards | M3_FINAL_VERIFICATION | M12 Hardening pass (DONE) |
| 17 | Startup Performance Acceleration & Zero-Lag Launch | Eliminated cmd.exe UTF-8 seek drift in `启动.bat`, removed blocking Google Fonts links in `index.html` preventing 15-30s browser stalls, added `-I` isolated mode & loopback proxy bypass for GPT-SoVITS engine, replaced browser launcher with 50ms raw loopback HTTPConnection probe, pre-seeded active voice profile in `lifespan` and eliminated redundant GPU model reloads in `switch_voice_profile` | M3_FINAL_VERIFICATION | Startup optimization pass (DONE) |
| 18 | Cross-Device Deployment Resilience & De-Bloat | Unified reference-audio path resolution (`resolve_existing_audio_path`/`to_project_relative_path` in `path_guard`, replacing 4 hand-rolled resolution sites; profiles now save project-relative paths for machine portability), single-source engine address parsed from `GPT_SOVITS_BASE_URL` replacing hardcoded 9880 in launcher, loopback-any-port CORS regex, memory guard sunk into `VoiceManager.switch_profile` with device-scaled threshold `max(1.0, total_gb*0.12)` (covers Telegram/auto-bind paths), cgroup detection consolidated into `utils/hardware.py` (-140 duplicated lines), platform-aware engine runtime interpreter candidates, dual-platform SILENT_AUDIO_ERROR guidance, legacy frontend routes kept by parallel session decision, safe-area composer padding | M3_FINAL_VERIFICATION | Cross-device optimization pass (DONE) |
| 19 | Evidence-Based Precision Calibration (Probe replaces GPU whitelist) | Deleted the `is_turing_tu116_tu117_gpu` GPU-model-name whitelist entirely (hardware.py, run_server.py branches, health telemetry `turing_fp32_active`→`fp32_forced`). New `utils/precision.py` store (`data/precision.json`, keyed to engine dir, BOM-safe). Launcher now: reads `GPT_SOVITS_PRECISION` env override > verified cache > FP16 default; after engine readiness synthesizes a one-sentence probe via `/tts` and checks `wav_peak_amplitude` — FP16 silent → auto-restarts engine with FP32 → re-probes → caches verified result. Inconclusive probes never cache. Refactored spawn into `_spawn_sovits_process` (reused for FP32 restart). No user knowledge required; any future GPU auto-adapts | M3_FINAL_VERIFICATION | Precision calibration pass (DONE) |
| 20 | Repository Quarantine & Asset Exclusion | Hardened `.gitignore` (all weight formats, temp audio, explicit characters recursion, data/audio_cache, temp_test/tmp_check, fix line 82 CRLF), update `scripts/package_release.py` | M4_HARDENING | Survey R1 (DONE) |
| 21 | 16GB RAM Memory Lifecycle Reclamation | `release_system_memory()` with `gc.collect()` + PyTorch empty_cache in `hardware.py`; invoked before memory guard and on model switch in `voice_manager.py` | M4_HARDENING | Survey R2 (DONE) |
| 22 | Frontend Memory & Listener De-allocation | Bounded LRU cache for audio blobs (50 entries) in `frontend/src/cache.js`, clean listener detachment via `curAudio.ontimeupdate` in `main.js`, rebuild assets | M4_HARDENING | Survey R2 (DONE) |
| 23 | URL Authority Userinfo Credential Masking | Sanitize `user:password@host` in `MaskingFilter.PATTERNS` in `utils/logger.py` to prevent credential leaks in stdout/file logs on connection errors | M4_HARDENING | Survey R3 (DONE) |
| 24 | Code Hygiene & v2.0 Documentation | Fix F821 undefined `updated` -> `updated_settings` in `routers/config.py:166`, remove dead imports, update `README.md` with AI Dynamic Voice, env vars, SPA settings | M4_HARDENING | Survey R4 (DONE) |
| 25 | Full Test Suite Calibration & 100% Pass Verification | Fix `test_adversarial_m2_challenger2.py:412` (style.css path) and `test_character_manager.py:472` (soundfile dependency), verify 100% tests pass | M4_HARDENING | Survey R5 (DONE) |
| 26 | CPU Inference Mode & Low-Memory GPU VRAM Inspection | `--cpu` CLI flag, `custom.device: cpu` YAML synchronization, `get_gpu_vram_status()` memory telemetry with <=4GB advisory, 3-state control panel toggle (FP16 ⇄ FP32 ⇄ CPU), and 100% test pass verification | M4_HARDENING | Performance & Stability (DONE) |
| 27 | Agile First-Sentence Chunking & TTS Pipeline Acceleration | Agile comma/clause pause segmentation for the first sentence chunk (>=6 chars), accelerating TTFB and speech output by 600-1200ms | M5_BACKEND_PIPELINE | Survey R1 (PLANNED) |
| 28 | GPT-SoVITS Prompt Audio Cache Warm-up & Audio Spec Cache | Asynchronous non-blocking warm-up during lifespan startup and profile switch to keep prompt_cache hot; cached audio sample specs | M5_BACKEND_PIPELINE | Survey R1 (PLANNED) |
| 29 | Web Audio API Gapless Streaming & Immediate First Chunk Playback | Web Audio API `StreamAudioController` with sample-accurate `source.start(nextStartTime)`, 12ms micro-fade boundary transitions, and immediate first chunk playback upon arrival | M6_FRONTEND_ENGINE | Survey R2 (PLANNED) |
| 30 | Smooth Fade-Out Interruption & Instant Queue Cancellation | 40ms smooth linear gain attenuation on user interrupt (stopBtn, switchSession, newChat, delete, sendMessage), instant queue purging and SSE abort | M6_FRONTEND_ENGINE | Survey R2 (PLANNED) |
| 31 | VRAM Watermark Guard, SSE Keep-Alive & Bounded Blob LRU Cache | Discrete GPU VRAM floor (0.45GB) before model switch, periodic W3C SSE keep-alive comments (`: keep-alive\n\n`) every 5s, bounded LRU Blob URL cache (cap 30) with explicit `URL.revokeObjectURL()` | M5_BACKEND_PIPELINE, M6_FRONTEND_ENGINE | Survey R3 (DONE) |
| 32 | Automated Quantitative Benchmark Suite & 100% Zero-Regression Verification | Dedicated quantitative benchmark module `test_benchmark_resilience_r4.py` measuring latency, 50-turn memory RSS drift (<35MB), mid-stream cancellation, and full 1079+ test regression | M7_GATE_VERIFICATION | Survey R4 (DONE) |
| 33 | Universal Provider UI & Dynamic Prefill | Redesign Model & Recognition panel: replace `#gCustomBox` with universal provider card across all 8 providers, API key masked/unmasked toggle with placeholder, Base URL custom input + one-click reset, preset dropdown + custom model input, dynamic prefill from backend | M8_UNIVERSAL_PROVIDER_UI | Survey R1 (IN_PROGRESS) |
| 34 | In-Flight Connectivity Testing & One-Click Activation | In-flight testing against `/api/providers/test` using freshly typed form values before saving; one-click save and activate as global active provider; deploy frontend via `npm run deploy` | M8_UNIVERSAL_PROVIDER_UI | Survey R1 (IN_PROGRESS) |
| 35 | Backend Provider Registry, xAI Grok & Groq Hardening | Remove `gpt-4o-mini` fallback trap, support HTTP 400 auth errors for xAI and Gemini in `test_connection()`, seed `groq` and `xai` in migrations and preset fallback | M9_BACKEND_DIAGNOSTICS_SETTINGS | Survey R1 (IN_PROGRESS) |
| 36 | Structured Error Interception & Chinese Guidance | Intercept Gemini 403/400, OpenAI 429, xAI 400/401, Groq 401/429, timeout, model not found with actionable Chinese guidance in `galgame2voice/utils/error_diagnostics.py` | M9_BACKEND_DIAGNOSTICS_SETTINGS | Survey R3 (IN_PROGRESS) |
| 37 | Settings Audit: STT Engine & Telegram Chat ID Schema Fix | Add `stt_engine` to `SettingsBase`, `SettingsUpdate`, `SettingsResponse`, SQLite schema; map `telegram_chat_id` alias to `telegram_admin_ids` | M9_BACKEND_DIAGNOSTICS_SETTINGS | Survey R2 (IN_PROGRESS) |
| 38 | Automated Provider Configuration & Key Protection Tests | Author `tests/test_provider_configuration_and_keys.py` covering all 8 providers, key masking, in-flight test mocks, real activation, xAI & Groq dedicated tests | M10_TEST_SUITE | Survey R4 (PLANNED) |
| 39 | Full Test Suite 100% Pass & Multi-Agent Gate Verification | Full `pytest tests/ -q` regression, 2 Reviewers, 2 Challengers, 1 Forensic Auditor integrity verification | M11_GATE_VERIFICATION | Survey R4 (PLANNED) |

---

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 0 | M0_E2E_TESTS | E2E Test Track: Design and publish comprehensive 4-tier opaque-box test suite (`TEST_READY.md`) | none | DONE |
| 1 | M1_SECURITY | Security & Zero-Leakage Hardening: Logger traceback masking, token redaction in HTTP/SSE/Telegram errors, relative path telemetry, memory prompt injection defense | none | DONE |
| 2 | M2_PERF_STABILITY | Performance & Stability Hardening: Microsecond TTS cache reordering, WAL shutdown checkpoint, test probe calibration for 100% test reliability | M1_SECURITY | DONE |
| 3 | M3_FINAL_VERIFICATION | Final Milestone: 100% E2E test pass across all tiers, adversarial challenger hardening (Tier 5), and Forensic Auditor integrity verification | M0_E2E_TESTS, M2_PERF_STABILITY | DONE |
| 4 | M4_HARDENING | Pre-Release Hardening & Repository Hygiene Finalization: R1-R5 implementations, zero model/database tracking, memory lifecycle reclamation, credential masking, code hygiene, README update, and 100% test pass rate | M3_FINAL_VERIFICATION | DONE |
| 5 | M5_BACKEND_PIPELINE | Backend Latency, Warm-up, VRAM Watermark Guard & SSE Keep-Alive | M4_HARDENING | DONE |
| 6 | M6_FRONTEND_ENGINE | Frontend Gapless Streaming, Immediate Playback, Interrupt Fade-out & Bounded Blob Cache | M4_HARDENING | DONE |
| 7 | M7_GATE_VERIFICATION | Multi-Agent Gate Verification & Integrity Audit for R4 Engineering pass | M5_BACKEND_PIPELINE, M6_FRONTEND_ENGINE | DONE |
| 8 | M8_UNIVERSAL_PROVIDER_UI | Frontend Universal Provider Form, API Key/Base URL/Model management, in-flight connectivity test, one-click activate, `npm run deploy` | none | IN_PROGRESS |
| 9 | M9_BACKEND_DIAGNOSTICS_SETTINGS | Backend xAI/Groq presets, `test_connection` gpt-4o-mini trap removal & HTTP 400 auth handling, structured Chinese error diagnostics, settings schema fix (stt_engine, telegram_chat_id) | none | IN_PROGRESS |
| 10 | M10_TEST_SUITE | Automated Test Suite: `tests/test_provider_configuration_and_keys.py`, 100% pass across all tests (`pytest tests/ -q`) | M8_UNIVERSAL_PROVIDER_UI, M9_BACKEND_DIAGNOSTICS_SETTINGS | PLANNED |
| 11 | M11_GATE_VERIFICATION | Gate Verification: 2 Reviewers, 2 Challengers, 1 Forensic Auditor, Sentinel victory report | M10_TEST_SUITE | PLANNED |


---

## Interface Contracts
### Logging MaskingFormatter Contract
- `MaskingFormatter`: Subclass of `logging.Formatter`. Overrides `format(record)` to run `MaskingFilter.sanitize()` on the fully formatted string (including `formatException`).
- `MaskingFilter.PATTERNS`: Regex patterns updated to match:
  1. `api_key=...`, `token=...`, `secret=...` (quoted or unquoted)
  2. `https://api.telegram.org/bot<token>/...` -> `https://api.telegram.org/bot***REDACTED***`
  3. `AIzaSy[A-Za-z0-9_-]{33}` (Google API Key)
  4. `hf_[A-Za-z0-9]{34}` (HuggingFace token)

### Error Sanitization Contract
- `sanitize_error_detail(exc: Union[Exception, str]) -> str`: Returns human-readable error description with all URL tokens, query parameters, API keys, and sensitive disk paths redacted. Used in `routers/config.py`, `routers/chat.py`, `services/chat_service.py`, `telegram_bot/handlers.py`.

### Database Telemetry Contract
- `GET /api/system/status`: `response["database"]["path"]` MUST return normalized relative path (e.g. `data/galgame2voice.db`), never absolute host path.

### Memory Extraction & Framing Contract
- `MemoryService.extract_facts_heuristic(text: str)`: Cleans input (strips newlines, tags, control chars, limits length to <= 50 chars).
- `MemoryService.format_memory_prompt_block(memories: List[CharacterMemory])`: Formats memory facts in defensive quotes with clear non-executable context markers.

### TTS Cache Latency Contract
- `TtsCacheManager.get(cache_key)`: Checks `self._mem_cache` first. If hit, returns immediately without disk I/O. Latency must measure `< 0.05ms`.

### Universal Provider API Contract
- `GET /api/providers`: Returns `{"providers": [...], "presets": [...]}`. Each entry contains `id`, `name`, `description`, `api_base_url`, `default_base_url`, `api_key` (masked `****` if present), `chat_model`, `preset_models`, `is_active`. If a preset is not yet in the DB, it is synthesized from `adapters/registry.py` presets.
- `POST /api/providers`: Upserts provider config. If `api_key` contains `****`, existing raw secret is preserved.
- `POST /api/providers/test`: Accepts `{id, api_base_url, api_key, chat_model}`. Tests connection using in-flight credentials (falling back to DB secret if key is masked or omitted). Returns `{"success": true, "latency_ms": ...}` or `{"success": false, "error": "...", "diagnostic": "..."}` with Chinese guidance.
- `POST /api/providers/{id}/activate`: Sets `is_active = 1` for provider and deactivates others.

### Error Diagnostics Contract
- `galgame2voice/utils/error_diagnostics.py`: `format_provider_error(provider_id: str, status_code: int, raw_error: str) -> dict`: Translates HTTP 400 (xAI invalid key, Gemini invalid key), 401 (unauthorized), 403 (region restriction / permission), 404 (model not found), 429 (rate/quota limit), timeouts, and connection errors into user-friendly Chinese recommendations. Eliminates raw tracebacks and secret leakage.

### Settings Schema Contract
- `SettingsBase`, `SettingsUpdate`, `SettingsResponse`: Contain `stt_engine: Optional[str] = "browser"` and `telegram_chat_id: Optional[str] = None` (aliased to `telegram_admin_ids`).
- SQLite table `settings`: Contains `stt_engine` column with default `'browser'`.

---

## Code Layout
- `galgame2voice/utils/logger.py`: Logging formatters and masking filters.
- `galgame2voice/routers/`: FastAPI routes (`chat.py`, `config.py`, `health.py`, `characters.py`, `audio.py`).
- `galgame2voice/services/`: Core business logic (`chat_service.py`, `tts_cache_manager.py`, `memory_service.py`, `gpt_sovits_client.py`, `session_manager.py`).
- `galgame2voice/database/`: SQLite engine, session, and CRUD operations (`session.py`, `crud.py`, `models.py`).
- `galgame2voice/telegram_bot/`: Telegram bot client and command/chat handlers (`bot.py`, `handlers.py`).
- `scripts/`: Launcher and maintenance scripts (`run_server.py`).
- `tests/`: Automated test suite (40+ test files, 750+ tests).
- `.agents/`: Agent coordination metadata (briefings, plans, progress, handoffs).
