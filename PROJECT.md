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
| 18 | Cross-Device Deployment Resilience & De-Bloat | Unified reference-audio path resolution (`resolve_existing_audio_path`/`to_project_relative_path` in `path_guard`, replacing 4 hand-rolled resolution sites; profiles now save project-relative paths for machine portability), single-source engine address parsed from `GPT_SOVITS_BASE_URL` replacing hardcoded 9880 in launcher, loopback-any-port CORS regex, memory guard sunk into `VoiceManager.switch_profile` with device-scaled threshold `max(1.0, total_gb*0.12)` (covers Telegram/auto-bind paths), cgroup detection consolidated into `utils/hardware.py` (-140 duplicated lines), platform-aware engine runtime interpreter candidates, FP32 warning for external engines on TU116/TU117 GPUs, dual-platform SILENT_AUDIO_ERROR guidance, removed legacy frontend (~2500 lines: `index_legacy.html`/`settings.html`/`static/js`/`static/css` + routes), safe-area composer padding | M3_FINAL_VERIFICATION | Cross-device optimization pass (DONE) |




---

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 0 | M0_E2E_TESTS | E2E Test Track: Design and publish comprehensive 4-tier opaque-box test suite (`TEST_READY.md`) | none | DONE |
| 1 | M1_SECURITY | Security & Zero-Leakage Hardening: Logger traceback masking, token redaction in HTTP/SSE/Telegram errors, relative path telemetry, memory prompt injection defense | none | DONE |
| 2 | M2_PERF_STABILITY | Performance & Stability Hardening: Microsecond TTS cache reordering, WAL shutdown checkpoint, test probe calibration for 100% test reliability | M1_SECURITY | DONE |
| 3 | M3_FINAL_VERIFICATION | Final Milestone: 100% E2E test pass across all tiers, adversarial challenger hardening (Tier 5), and Forensic Auditor integrity verification | M0_E2E_TESTS, M2_PERF_STABILITY | DONE |

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
