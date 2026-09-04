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
| 9 | 100% E2E Regression Pass & Adversarial Hardening (Tier 5) | Verify 100% pass of E2E suite, perform adversarial stress testing and forensic integrity verification | M3_FINAL_VERIFICATION | Survey R4 |

---

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 0 | M0_E2E_TESTS | E2E Test Track: Design and publish comprehensive 4-tier opaque-box test suite (`TEST_READY.md`) | none | DONE |
| 1 | M1_SECURITY | Security & Zero-Leakage Hardening: Logger traceback masking, token redaction in HTTP/SSE/Telegram errors, relative path telemetry, memory prompt injection defense | none | DONE |
| 2 | M2_PERF_STABILITY | Performance & Stability Hardening: Microsecond TTS cache reordering, WAL shutdown checkpoint, test probe calibration for 100% test reliability | M1_SECURITY | DONE |
| 3 | M3_FINAL_VERIFICATION | Final Milestone: 100% E2E test pass across all tiers, adversarial challenger hardening (Tier 5), and Forensic Auditor integrity verification | M0_E2E_TESTS, M2_PERF_STABILITY | PLANNED |

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
