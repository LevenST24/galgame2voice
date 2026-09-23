# Galgame2Voice Test Infrastructure & 4-Tier Opaque-Box Strategy

## 1. Executive Summary & Testing Philosophy

Galgame2Voice is an industrial-grade local AI Galgame companion and TTS voice streaming server designed for 24/7 continuous operation under extreme concurrency. To ensure absolute engineering resilience, zero information leakage, sub-millisecond memory latency, and bulletproof fault tolerance, the testing framework adheres strictly to an **Opaque-Box Testing Strategy** across four distinct operational tiers.

### Core Testing Tenets:
1. **Opaque-Box Contract Verification**: Tests evaluate system behaviors solely through public APIs, HTTP/SSE protocols, exported service interfaces, and CLI boundaries without depending on internal implementation quirks.
2. **Deterministic Derivation**: Every test assertion is derived from unambiguous requirements in `PROJECT.md` and `ORIGINAL_REQUEST.md`.
3. **No Facade or Flaky Tests**: Tests perform real compute, file I/O, database WAL transactions, and concurrency stress without mock bypasses that fabricate passing results.
4. **Adversarial & Chaos Hardening**: Explicit injection of malicious payloads, path traversal attempts, prompt injection delimiters, connection drops, and burst I/O locks.

---

## 2. The 4-Tier Test Architecture

```
+-------------------------------------------------------------------------+
|                  Tier 4: Real-World Application Scenarios               |
|   (Full-lifecycle user journeys, sustained streaming sessions, chaos)   |
+-------------------------------------------------------------------------+
                                    |
+-------------------------------------------------------------------------+
|                Tier 3: Cross-Feature Combinations (Pairwise)            |
|   (Concurrent WAL + Cache, Streaming + Injection, Auth + Proxy Errors)   |
+-------------------------------------------------------------------------+
                                    |
+-------------------------------------------------------------------------+
|                 Tier 2: Boundary & Corner Cases (>=5/feature)           |
| (Empty inputs, extreme payloads, Unicode overflows, corrupt files, zero) |
+-------------------------------------------------------------------------+
                                    |
+-------------------------------------------------------------------------+
|                    Tier 1: Feature Coverage (>=5/feature)               |
|      (Happy-path contract compliance for F1-F9 functional domains)      |
+-------------------------------------------------------------------------+
```

---

## 3. Tier 1: Feature Coverage Matrix (>= 5 Tests Per Feature)

Every functional domain is validated against its primary functional contracts:

### F1: Security Logging Masking & Traceback Sanitization
- `T1_LOG_01`: Masks standard OpenAI API keys (`sk-proj-...`, `sk-...`).
- `T1_LOG_02`: Masks Google Gemini API keys (`AIzaSy...`).
- `T1_LOG_03`: Masks HuggingFace user tokens (`hf_...`).
- `T1_LOG_04`: Masks Telegram Bot tokens in URLs and logs (`bot<token>`).
- `T1_LOG_05`: Redacts exception tracebacks and dictionary arguments containing passwords/secrets.

### F2: Error Detail & Token Sanitization in Responses
- `T1_ERR_01`: HTTP 4xx/5xx error responses contain zero unmasked API keys.
- `T1_ERR_02`: SSE streaming `event: error` JSON payloads contain sanitized error messages.
- `T1_ERR_03`: Telegram connection test endpoint masks tokens in failure messages.
- `T1_ERR_04`: Provider connection test endpoint prevents token disclosure during HTTP timeouts.
- `T1_ERR_05`: File browse/dialog endpoints sanitize underlying OS filesystem exceptions.

### F3: System Diagnostics & Health Telemetry
- `T1_HLT_01`: `GET /api/health` returns HTTP 200 with accurate uptime and version.
- `T1_HLT_02`: `GET /status` returns valid GPT-SoVITS reachability status.
- `T1_HLT_03`: `GET /api/system/status` returns normalized database relative path.
- `T1_HLT_04`: Storage telemetry accurately computes cached audio directory file count and MB.
- `T1_HLT_05`: System status gathers parallel telemetry without blocking the async event loop.

### F4: Memory Prompt Injection Defense & Fact Extraction
- `T1_MEM_01`: Heuristic extraction extracts user nickname (`player_name`).
- `T1_MEM_02`: Heuristic extraction extracts user preferences (`like_...`, `dislike_...`).
- `T1_MEM_03`: Heuristic extraction extracts user promises and appointments.
- `T1_MEM_04`: Heuristic extraction extracts user occupation/identity.
- `T1_MEM_05`: `format_memory_prompt_block` embeds extracted facts in defensive prompt frames.

### F5: Two-Tier TTS Cache Microsecond Latency & Atomic Persistence
- `T1_TTS_01`: Deterministic SHA256 key computation from text and canonical inference options.
- `T1_TTS_02`: In-memory LRU cache hit returns audio in `< 0.05ms`.
- `T1_TTS_03`: Atomic file persistence prevents incomplete or corrupted disk artifacts.
- `T1_TTS_04`: SQLite metadata indexing records audio duration, file size, and timestamps.
- `T1_TTS_05`: Cache eviction prunes oldest entries down to 80% capacity limit when thresholds exceed.

### F6: Database SQLite WAL Concurrency & Burst R/W
- `T1_DB_01`: Database connection initializes in WAL journal mode with `busy_timeout=5000`.
- `T1_DB_02`: Concurrent asynchronous reads and writes execute without `database is locked`.
- `T1_DB_03`: Settings CRUD properly upserts and retrieves masked/unmasked credentials.
- `T1_DB_04`: Provider CRUD maintains multiple provider profiles with default fallbacks.
- `T1_DB_05`: Message history CRUD safely limits and retrieves recent conversation turns.

### F7: Path Traversal & Audio File Security
- `T1_SEC_01`: Rejects `../` path traversal attempts in static audio endpoints.
- `T1_SEC_02`: Rejects absolute path escapes and URL encoded `%2e%2e%2f` sequences.
- `T1_SEC_03`: `fs-browse` endpoint restricts access and filters by whitelisted extensions (`.ckpt`, `.pth`, `.wav`).
- `T1_SEC_04`: SSRF protection blocks private/loopback IP requests for external LLM base URLs.
- `T1_SEC_05`: Temporary cache write names isolate pid and timestamp to avoid collisions.

### F8: SSE Streaming Bilingual Pipeline
- `T1_SSE_01`: Formats SSE chunks adhering to standard `event: ...\ndata: ...\n\n` specification.
- `T1_SSE_02`: Real-time bilingual parser extracts Chinese stream tokens for UI display.
- `T1_SSE_03`: Lookahead Japanese parser extracts complete sentences for TTS synthesis queue.
- `T1_SSE_04`: Pipelined consumer generates audio chunks concurrently with token generation.
- `T1_SSE_05`: Client cancellation safely terminates upstream generator without memory leaks.

### F9: Telegram Bot Multi-User Isolation & Entity Safety
- `T1_TG_01`: Markdown/HTML entity escaping prevents parsing crashes on special characters.
- `T1_TG_02`: Multi-user sessions maintain isolated memory and affection states.
- `T1_TG_03`: Per-user task interruption cancels ongoing generation when new prompt arrives.
- `T1_TG_04`: Dynamic proxy routing supports SOCKS5/HTTP proxies with failover.
- `T1_TG_05`: Hot-reloading Telegram credentials gracefully restarts polling task.

---

## 4. Tier 2: Boundary & Corner Cases (>= 5 Tests Per Feature)

Tier 2 exposes the system to extreme inputs, edge values, resource constraints, and protocol violations:

### F1: Logging Masking Boundary Cases
- `T2_LOG_01`: Empty, whitespace-only, and single-character log messages.
- `T2_LOG_02`: Massive 1MB log strings with nested secrets and repeated patterns.
- `T2_LOG_03`: Multiple secrets concatenated without delimiters (`sk-1234567890AIzaSy1234567890`).
- `T2_LOG_04`: Log records with complex non-string arguments (custom objects, tuples, nested dicts).
- `T2_LOG_05`: Secrets embedded inside URL query parameters and JSON payloads simultaneously.

### F2: Error Sanitization Boundary Cases
- `T2_ERR_01`: Nested exception chains with recursive `__cause__` and `__context__`.
- `T2_ERR_02`: Custom exception classes with overridden `__str__` containing raw API keys.
- `T2_ERR_03`: Malformed HTTP error responses with non-UTF8 binary data.
- `T2_ERR_04`: SSE stream error events with embedded newlines, null bytes, and JSON control chars.
- `T2_ERR_05`: OS permission error containing sensitive local user path hierarchies.

### F3: System Telemetry Boundary Cases
- `T3_HLT_01`: Missing or zero-byte database file state transitions.
- `T3_HLT_02`: Storage scan with 10,000+ files and deeply nested subdirectories.
- `T3_HLT_03`: Upstream GPT-SoVITS server experiencing 100% packet drop (connect timeout).
- `T3_HLT_04`: System status called when psutil/memory probes are unavailable.
- `T3_HLT_05`: High-frequency polling (20 requests/sec) to verify TTL caching avoids disk thrashing.

### F4: Memory Injection Boundary Cases
- `T4_MEM_01`: Input containing prompt injection tags (`【System Override】`, ````markdown`, `\n\nHuman:`).
- `T4_MEM_02`: Input containing 10,000 repeating characters designed to cause regex ReDoS.
- `T4_MEM_03`: Memory extraction on mixed CJK, emojis, Cyrillic, and RTL Hebrew text.
- `T4_MEM_04`: Memory fact extraction with 0 confidence or ambiguous patterns.
- `T4_MEM_05`: Attempting to inject system instructions inside user preference statements.

### F5: TTS Cache Boundary Cases
- `T5_TTS_01`: Zero-byte WAV file recovery: automatically unlinks and re-synthesizes.
- `T5_TTS_02`: Cache key computation with unicode text, Japanese katakana/hiragana, and punctuation.
- `T5_TTS_03`: Cache put with extreme parameters (speed=0.1, speed=3.0, top_k=1, top_k=100).
- `T5_TTS_04`: In-memory LRU byte cap overflow: massive audio files forcing multiple evictions.
- `T5_TTS_05`: Sudden disk full / read-only filesystem handling during cache write.

### F6: Database Concurrency Boundary Cases
- `T6_DB_01`: 50 concurrent transactions writing simultaneously under tight loop.
- `T6_DB_02`: Database file locked by external process with busy handler timeout recovery.
- `T6_DB_03`: Inserting maximum length strings (4,000 chars prompt, 1,000 chars response).
- `T6_DB_04`: SQLite table schema migration idempotency check.
- `T6_DB_05`: Transaction rollback verification on mid-flight async cancellation.

### F7: Security & Traversal Boundary Cases
- `T7_SEC_01`: Windows drive path traversal (`C:\Windows\System32\drivers\etc\hosts`).
- `T7_SEC_02`: UNC path traversal (`\\127.0.0.1\c$\secret.txt`).
- `T7_SEC_03`: Null byte injection in file paths (`audio.wav\0secret.txt`).
- `T7_SEC_04`: Alternate Data Streams (`test.wav::$DATA`).
- `T7_SEC_05`: SSRF with decimal/octal/hex IP encodings (`http://2130706433/`).

### F8: Streaming Pipeline Boundary Cases
- `T8_SSE_01`: LLM stream outputting 1 token per 5 seconds (slow connection).
- `T8_SSE_02`: LLM stream sending 100,000 tokens in a single burst.
- `T8_SSE_03`: Japanese text with no punctuation marks (lookahead buffer flush).
- `T8_SSE_04`: Pure ASCII text with no Japanese or Chinese characters.
- `T8_SSE_05`: Immediate client abort after initial byte received.

### F9: Telegram Bot Boundary Cases
- `T9_TG_01`: Message containing raw unescaped HTML (`<script>`, `<b>` without closing tag).
- `T9_TG_02`: Message containing unescaped MarkdownV2 special characters (`_`, `*`, `[`, `]`, `(`, `)`).
- `T9_TG_03`: User sending rapid barrage of 20 messages in 1 second.
- `T9_TG_04`: Telegram webhook receiving malformed update JSON.
- `T9_TG_05`: Network disconnect during audio file voice note upload.

---

## 5. Tier 3: Cross-Feature Combinations (Pairwise Testing Matrix)

Pairwise testing validates subsystem boundaries where multiple features interact simultaneously:

| Test ID | Subsystem A | Subsystem B | Interaction Scenario |
|---|---|---|---|
| `T3_PAIR_01` | F4 Memory Injection | F8 SSE Streaming | Malicious prompt injected into SSE stream; verify facts are safely extracted, sanitized, and streamed without delimiter corruption. |
| `T3_PAIR_02` | F5 TTS Cache | F6 Database WAL | 50 concurrent threads querying and writing to TTS Cache while SQLite WAL writes are under heavy burst load. |
| `T3_PAIR_03` | F1 Log Masking | F2 Error Responses | Upstream API failure with secret key; verify both HTTP response body and log records mask all tokens without double-encoding. |
| `T3_PAIR_04` | F7 Path Traversal | F5 TTS Cache | Malicious cache key containing `../../` attempted; verify sanitized into canonical SHA256 hex before filesystem stat. |
| `T3_PAIR_05` | F3 System Telemetry | F6 Database WAL | System status requested simultaneously during a heavy database checkpoint operation; verify zero lock contention. |
| `T3_PAIR_06` | F9 Telegram Bot | F4 Memory & Affection | Telegram user changes nicknames and triggers memory extraction; verify affection levels and nicknames update atomically in SQLite. |
| `T3_PAIR_07` | F8 SSE Streaming | F5 TTS Cache | Real-time dialogue sentences hit pre-cached entries in memory; verify seamless stream synthesis interleaving. |
| `T3_PAIR_08` | F7 SSRF Guard | F2 Error Responses | SSRF attempt to `http://169.254.169.254/` blocked; verify HTTP 400 detail explains restriction without leaking internal networking. |
| `T3_PAIR_09` | F9 Telegram Bot | F1 Log Masking | Telegram bot polling fails with 401 Unauthorized; verify bot token in Telegram URL is masked in logger. |
| `T3_PAIR_10` | F5 TTS Cache | F7 Path Traversal | Audio cleanup worker scans `audio/` directory; verify `audio/cache/` is strictly protected while invalid traversal files are rejected. |

---

## 6. Tier 4: Real-World Application Scenarios

Tier 4 simulates realistic, end-to-end user workflows and real production workloads:

### Scenario 1: Multi-Turn Galgame Session with Evolving Memory & Affection
- **Workflow**:
  1. User introduces themselves: "你好，我叫翔太，是一名程序员。" -> Facts extracted: `player_name="翔太"`, `occupation="程序员"`.
  2. Character responds warmly, updating affection score from Level 1 to Level 2.
  3. User states preference: "我最喜欢吃拉面和甜甜圈。" -> Fact extracted: `like_拉面和甜甜圈`.
  4. Next turn: "今天下班好累啊。" -> System retrieves memories `翔太`, `程序员`, `拉面` and injects them into system prompt block.
  5. Character mentions eating ramen together after programming work.
- **Verification**: Database states, memory retrieval composite score, affection transitions, and SSE tokens.

### Scenario 2: High-Concurrency Burst Multi-Tenant Chat
- **Workflow**:
  1. 10 simultaneous virtual users start SSE streaming sessions across different session IDs.
  2. Synthesizer generates audio chunks in parallel with token streaming.
  3. Shared GPT-SoVITS mutex synchronizes inference while memory LRU cache serves repeated dialogue phrases in `< 0.05ms`.
- **Verification**: Zero deadlocks, zero dropped SSE connections, 100% response completion, total throughput stability.

### Scenario 3: Upstream Chaos & Graceful Degradation
- **Workflow**:
  1. LLM provider suddenly times out after 100ms.
  2. TTS backend returns 502 Bad Gateway.
  3. Telegram Bot loses internet connection.
- **Verification**: FastAPI emits structured fallback error events, log files record masked alerts, database transactions roll back cleanly, service remains 100% operational for subsequent requests.

### Scenario 4: Dynamic Configuration & Model Hot-Reloading
- **Workflow**:
  1. Admin updates GPT-SoVITS endpoint and switches active voice profile via REST API.
  2. Admin enables Telegram proxy and updates bot credentials.
  3. Ongoing user chat sessions continue without interruption.
- **Verification**: Active voice profile switches with automatic rollback on error; Telegram bot restarts polling automatically with new credentials.

### Scenario 5: Cache Saturation & Continuous Pruning Under Load
- **Workflow**:
  1. Cache directory is populated with 5,000 synthesized audio entries exceeding max configured capacity (1024MB).
  2. Background pruning triggers, calculating least-recently-used entries.
  3. Pruner deletes oldest files and purges SQLite metadata down to 80% watermark.
- **Verification**: High-priority frequent audio entries remain in memory/disk; total cache storage stays strictly within bounds.

---

## 7. Test Execution & CI Automation

### Test Runner Commands:
```powershell
# Run the entire automated test suite
python -m pytest

# Run industrial hardening and E2E regression tests specifically
python -m pytest tests/test_e2e_industrial_hardening.py -v

# Run with coverage report
python -m pytest --cov=galgame2voice --cov-report=term-missing
```

### Flakiness Mitigation Standards:
- **Zero Fixed Sleeps**: All asynchronous wait states utilize `asyncio.Event`, `asyncio.wait_for`, or bounded polling loops.
- **Isolated SQLite Instances**: Every test run uses isolated SQLite in-memory or temporary disk databases (`tmp_path`).
- **Deterministic Time Mocking**: High-precision timers (`time.perf_counter()`, `time.monotonic()`) are used for microsecond benchmarking.
