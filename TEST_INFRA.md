# Galgame2Voice Test Infrastructure & 4-Tier Opaque-Box Strategy

## 1. Executive Summary & Testing Philosophy

Galgame2Voice is an industrial-grade local AI Galgame companion and TTS voice streaming server designed for 24/7 continuous operation under extreme concurrency. To ensure absolute engineering resilience, zero information leakage, sub-millisecond memory latency, and bulletproof fault tolerance, the testing framework adheres strictly to an **Opaque-Box Testing Strategy** across four distinct operational tiers.

### Core Testing Tenets:
1. **Opaque-Box Contract Verification**: Tests evaluate system behaviors solely through public APIs, HTTP/SSE protocols, exported service interfaces, and CLI boundaries without depending on internal implementation quirks.
2. **Deterministic Derivation**: Every test assertion is derived from unambiguous requirements in `PROJECT.md` (Feature Inventory & Interface Contracts). The formerly referenced `ORIGINAL_REQUEST.md` does not exist in this repository.
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
|               Tier 2: Boundary & Corner Cases (15 tests)                |
| (Empty inputs, extreme payloads, Unicode overflows, corrupt files, zero) |
+-------------------------------------------------------------------------+
                                    |
+-------------------------------------------------------------------------+
|                   Tier 1: Feature Coverage (37 tests)                   |
|      (Happy-path contract compliance for F1-F9 functional domains)      |
+-------------------------------------------------------------------------+
```

---

## 3. Tier 1: Feature Coverage Matrix (37 Tests Implemented)

Implemented in `TestTier1FeatureCoverage` (`tests/test_e2e_industrial_hardening.py`): 37 tests
total — 5 each for F1–F6, 4 for F7, 1 for F8, 2 for F9. The `T1_*` identifiers below are
specification IDs only: no test file, pytest marker, or node ID in this repository contains
them. The actual methods are named `test_f<domain>_<nn>_<slug>`; a single method may assert
several contract lines, contract lines do not map 1:1 onto method numbers, and any contract
line beyond a domain's implemented count listed above has no Tier-1 test method (the F7/F8/F9
notes below say where each of them is covered instead).

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
- `T1_SEC_03`: `fs-browse` endpoint restricts access and filters by the per-`file_type` whitelists in `_FS_BROWSE_EXTS` (`gpt` → `.ckpt`, `sovits` → `.pth`, `audio` → `.wav`/`.ogg`/`.mp3`/`.flac`/`.m4a`).
- `T1_SEC_04`: SSRF protection blocks private/loopback IP requests for external LLM base URLs.
- `T1_SEC_05`: Temporary cache write names isolate pid and timestamp to avoid collisions.

**Implemented (4 tests)**: `test_f7_01_reject_audio_path_traversal` (item 1), `test_f7_02_fs_browse_filter_extensions`
(item 3), `test_f7_03_ssrf_url_guard_blocks_private_ranges` and `test_f7_04_ssrf_url_guard_allows_public_https` (item 4).
Item 2 is asserted outside Tier 1 — `tests/test_path_guard_and_converter.py` (`contains_traversal_payload("%2e%2e/passwd")`)
and `tests/test_adversarial_m1_security_deep_challenger.py` (HTTP requests against the `/audio` mount with
`%2e%2e%2f`, `..%2f`, `..%5c`) — and item 5 has no test anywhere in the suite; the naming scheme itself lives in
`galgame2voice/services/tts_cache_manager.py` (`_write_audio_file_sync`).

### F8: SSE Streaming Bilingual Pipeline
- `T1_SSE_01`: Formats SSE chunks adhering to standard `event: ...\ndata: ...\n\n` specification.
- `T1_SSE_02`: Real-time bilingual parser extracts Chinese stream tokens for UI display.
- `T1_SSE_03`: Lookahead Japanese parser extracts complete sentences for TTS synthesis queue.
- `T1_SSE_04`: Pipelined consumer generates audio chunks concurrently with token generation.
- `T1_SSE_05`: Client cancellation safely terminates upstream generator without memory leaks.

**Implemented (1 test)**: `test_f8_01_streaming_bilingual_parser_incremental` asserts items 2 and 3. Item 1's
`event:`/`data:` framing is asserted by `test_f2_02_sse_error_event_formatting` (same class, filed under F2), and
items 4 and 5 are covered outside Tier 1 by `tests/test_m14_real_streaming_and_scheduler.py`
(`test_true_streaming_pipelining_and_early_lock_release`, `test_tts_scheduler_priority_and_cancellation`) and
`tests/test_benchmark_resilience_r4.py` (`test_r2_stream_chat_cancellation_latency_under_100ms`).

### F9: Telegram Bot Multi-User Isolation & Entity Safety
- `T1_TG_01`: Markdown/HTML entity escaping prevents parsing crashes on special characters.
- `T1_TG_02`: Multi-user sessions maintain isolated memory and affection states.
- `T1_TG_03`: Per-user task interruption cancels ongoing generation when new prompt arrives.
- `T1_TG_04`: Dynamic proxy routing supports SOCKS5/HTTP proxies with failover.
- `T1_TG_05`: Hot-reloading Telegram credentials gracefully restarts polling task.

**Implemented (2 tests)**: `test_f9_01_telegram_session_keys_isolated` (item 2) and `test_f9_02_telegram_task_cancellation`
(item 3). Item 4's SOCKS5/HTTP routing is asserted by `tests/test_telegram_bot.py::test_proxy_helpers`
(URL normalization and request kwargs; failover itself is not asserted there). Item 1 has no Tier-1 method, and the
closest match, `tests/test_telegram_bot.py::test_telegram_text_escaping_safety`, only asserts on the raw sample string
without invoking any escaping path — see the Tier-2 F9 note: the bot sends plain text and no entity-escaping layer
exists in `galgame2voice/telegram_bot/`. Item 5 has no test either: `_reload_telegram_if_needed` in
`galgame2voice/routers/config.py` implements the restart, but the suite only asserts that the persisted
`telegram_enabled` flag flips (`tests/test_telegram_bot.py::test_config_api_toggles_telegram_enabled`).

---

## 4. Tier 2: Boundary & Corner Cases (15 Tests Implemented)

Implemented in `TestTier2BoundaryAndCornerCases` (`tests/test_e2e_industrial_hardening.py`): 15 tests
total — 4 for F1 (`test_b1_01`–`test_b1_04`), 2 for F2 (`test_b2_01`–`test_b2_02`), 3 for F4
(`test_b4_01`–`test_b4_03`), 3 for F5 (`test_b5_01`–`test_b5_03`), 2 for F6 (`test_b6_01`–`test_b6_02`),
1 for F7 (`test_b7_01`), and **none** for F3, F8 and F9 — no `test_b3_*`, `test_b8_*` or `test_b9_*` method
exists anywhere in `tests/`. As in Tier 1, the `T2_*`…`T9_*` identifiers below are specification IDs that no
test carries; the leading digit of each ID family is a per-domain prefix (`T2_LOG`, `T2_ERR`, `T3_HLT`,
`T4_MEM`, `T5_TTS`, `T6_DB`, `T7_SEC`, `T8_SSE`, `T9_TG`) and does not correspond to a test method number.

Tier 2 exposes the system to extreme inputs, edge values, resource constraints, and protocol violations:

### F1: Logging Masking Boundary Cases
- `T2_LOG_01`: Empty, whitespace-only, and single-character log messages.
- `T2_LOG_02`: Massive 1MB log strings with nested secrets and repeated patterns.
- `T2_LOG_03`: Multiple secrets concatenated without delimiters (`sk-1234567890AIzaSy1234567890`).
- `T2_LOG_04`: Log records with complex non-string arguments (custom objects, tuples, nested dicts).
- `T2_LOG_05`: Secrets embedded inside URL query parameters and JSON payloads simultaneously.

**Implemented (4 of the 5 listed)**: `test_b1_01`–`test_b1_04`. Item 5 has no Tier-2 test method, and item 2's
scale is an order of magnitude smaller than specified: `test_b1_02` sanitizes a ~114KB string
(`"Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 1000` on both sides of one embedded key), not 1MB.

### F2: Error Sanitization Boundary Cases
- `T2_ERR_01`: Nested exception chains with recursive `__cause__` and `__context__`.
- `T2_ERR_02`: Custom exception classes with overridden `__str__` containing raw API keys.
- `T2_ERR_03`: Malformed HTTP error responses with non-UTF8 binary data.
- `T2_ERR_04`: SSE stream error events with embedded newlines, null bytes, and JSON control chars.
- `T2_ERR_05`: OS permission error containing sensitive local user path hierarchies.

**Implemented (2 of the 5 listed)**: `test_b2_01`–`test_b2_02`. Items 3–5 have no Tier-2 test method
(permission-denied filesystem browse results are asserted in `tests/test_adversarial_m1_security_deep_challenger.py`).

### F3: System Telemetry Boundary Cases
- `T3_HLT_01`: Missing or zero-byte database file state transitions.
- `T3_HLT_02`: Storage scan with 10,000+ files and deeply nested subdirectories.
- `T3_HLT_03`: Upstream GPT-SoVITS server experiencing 100% packet drop (connect timeout).
- `T3_HLT_04`: System status called when psutil/memory probes are unavailable.
- `T3_HLT_05`: High-frequency polling (20 requests/sec) to verify TTL caching avoids disk thrashing.

**Specified only**: `TestTier2BoundaryAndCornerCases` contains no `test_b3_*` method — none of items 1–5 has a
Tier-2 test.

### F4: Memory Injection Boundary Cases
- `T4_MEM_01`: Input containing prompt injection tags (`【System Override】`, ````markdown`, `\n\nHuman:`).
- `T4_MEM_02`: Input containing 10,000 repeating characters designed to cause regex ReDoS.
- `T4_MEM_03`: Memory extraction on mixed CJK, emojis, Cyrillic, and RTL Hebrew text.
- `T4_MEM_04`: Memory fact extraction with 0 confidence or ambiguous patterns.
- `T4_MEM_05`: Attempting to inject system instructions inside user preference statements.

**Implemented (3 of the 5 listed)**: `test_b4_01`–`test_b4_03`. Items 4–5 have no Tier-2 test method.

### F5: TTS Cache Boundary Cases
- `T5_TTS_01`: Zero-byte WAV file recovery: automatically unlinks and re-synthesizes.
- `T5_TTS_02`: Cache key computation with unicode text, Japanese katakana/hiragana, and punctuation.
- `T5_TTS_03`: Cache put with extreme parameters (speed=0.1, speed=3.0, top_k=1, top_k=100).
- `T5_TTS_04`: In-memory LRU byte cap overflow: massive audio files forcing multiple evictions.
- `T5_TTS_05`: Sudden disk full / read-only filesystem handling during cache write.

**Implemented (3 of the 5 listed)**: `test_b5_01` (item 1), `test_b5_03` (item 3), `test_b5_02` (item 4).
Item 2's unicode/Japanese key computation is asserted one tier up by `test_f5_01_cache_key_computation_deterministic`,
and item 5 has no test: no method simulates a full or read-only filesystem during a cache **write**
(`tests/test_adversarial_m2_cache.py` injects an `OSError` on `Path.read_bytes`, which is a read failure, and
`tests/test_audit_hardening_m8.py` injects a `PermissionError` on `Path.unlink`).

### F6: Database Concurrency Boundary Cases
- `T6_DB_01`: 50 concurrent transactions writing simultaneously under tight loop.
- `T6_DB_02`: Database file locked by external process with busy handler timeout recovery.
- `T6_DB_03`: Inserting maximum length strings (4,000 chars prompt, 1,000 chars response).
- `T6_DB_04`: SQLite table schema migration idempotency check.
- `T6_DB_05`: Transaction rollback verification on mid-flight async cancellation.

**Implemented (2 of the 5 listed)**: `test_b6_01_database_burst_concurrency_stress` (item 1, but it drives **30**
concurrent inserts, not 50) and `test_b6_02_database_max_payload_strings` (item 3, but only its prompt half — it
inserts a 4,000-character `content_chinese` against a 5-character `content_japanese`; no 1,000-char response bound
exists, since `MessageBase` declares no `max_length` on either content field). Items 2 and 4 have no Tier-2
test method; the nearest coverage of item 5's rollback atomicity is
`tests/test_immediate_transaction_nesting.py::test_inner_rollback_does_not_discard_outer_changes`.

### F7: Security & Traversal Boundary Cases
- `T7_SEC_01`: Windows drive path traversal (`C:\Windows\System32\drivers\etc\hosts`).
- `T7_SEC_02`: UNC path traversal (`\\127.0.0.1\c$\secret.txt`).
- `T7_SEC_03`: Null byte injection in file paths (`audio.wav\0secret.txt`).
- `T7_SEC_04`: Alternate Data Streams (`test.wav::$DATA`).
- `T7_SEC_05`: SSRF with decimal/octal/hex IP encodings (`http://2130706433/`).

**Implemented (1 of the 5 listed)**: `test_b7_01_path_traversal_windows_device_names` — despite its name it asserts
that `validate_llm_base_url` rejects non-http schemes (`file://`, `ftp://`) and empty/whitespace hosts, not the
traversal vectors listed here. Items 1–3 are asserted outside Tier 2 by `tests/test_path_guard_and_converter.py`
(`..\windows\system32`, `audio\x00/file.wav`, UNC shares) and `tests/test_adversarial_m1_security_deep_challenger.py`
(`HTTP GET /audio/C:/Windows/win.ini` and `c:\windows\system32\calc.exe` against the static mount). Items 4 and 5
have no test: no file under `tests/` contains `$DATA` or a decimal/octal/hex-encoded loopback literal.

### F8: Streaming Pipeline Boundary Cases
- `T8_SSE_01`: LLM stream outputting 1 token per 5 seconds (slow connection).
- `T8_SSE_02`: LLM stream sending 100,000 tokens in a single burst.
- `T8_SSE_03`: Japanese text with no punctuation marks (lookahead buffer flush).
- `T8_SSE_04`: Pure ASCII text with no Japanese or Chinese characters.
- `T8_SSE_05`: Immediate client abort after initial byte received.

**Specified only**: `TestTier2BoundaryAndCornerCases` contains no `test_b8_*` method. Parser-level pathological inputs
for this domain are exercised elsewhere (`tests/test_adversarial_stress_r4_challenger.py`,
`tests/test_m2_adversarial_challenger1.py`). Item 1's timed slow drip has no Tier-2 test (the closest is a slow
streaming adapter in `tests/test_benchmark_resilience_r4.py`, used to measure cancellation latency), and item 2's
100,000-token stream burst has none either — the nearest analogue,
`tests/test_adversarial_m6_challenger1.py::test_single_giant_turn_exceeding_budget`, pushes a 100,000-character turn
through `SessionManager`'s token budget rather than through the stream parser.

### F9: Telegram Bot Boundary Cases
- `T9_TG_01`: Message containing raw unescaped HTML (`<script>`, `<b>` without closing tag).
- `T9_TG_02`: Message containing unescaped MarkdownV2 special characters (`_`, `*`, `[`, `]`, `(`, `)`).
- `T9_TG_03`: User sending rapid barrage of 20 messages in 1 second.
- `T9_TG_04`: Telegram webhook receiving malformed update JSON.
- `T9_TG_05`: Network disconnect during audio file voice note upload.

**Specified only**: `TestTier2BoundaryAndCornerCases` contains no `test_b9_*` method. Item 3's nearest analogue is
`tests/test_telegram_bot.py::test_concurrent_users_handling`, which drives two users concurrently rather than a
20-message barrage from one; item 5's nearest analogue is `test_corrupt_voice_file_error_reply`, which feeds a corrupt
payload instead of dropping the network mid-upload. Items 1–2 have no test and no subject: no file under
`galgame2voice/` sets `parse_mode` and `galgame2voice/telegram_bot/` contains no Markdown/HTML entity-escaping
code, so replies go out as plain text. Item 4 also has no subject — the bot is long-polling only and the repository
defines no Telegram webhook endpoint.

---

## 5. Tier 3: Cross-Feature Combinations (Pairwise Testing Matrix)

Pairwise testing validates subsystem boundaries where multiple features interact simultaneously.
`TestTier3CrossFeatureCombinations` implements **5** of the 10 combinations listed below (`test_pair_01`–`test_pair_05`);
the `T3_PAIR_*` IDs are specification IDs that no test carries, and the fifth implemented test corresponds to table row
06, not row 05:

| Test ID | Subsystem A | Subsystem B | Interaction Scenario | Implemented As |
|---|---|---|---|---|
| `T3_PAIR_01` | F4 Memory Injection | F8 SSE Streaming | Malicious prompt injected into SSE stream; verify facts are safely extracted, sanitized, and streamed without delimiter corruption. | `test_pair_01_memory_injection_and_bilingual_streaming` |
| `T3_PAIR_02` | F5 TTS Cache | F6 Database WAL | 50 concurrent threads querying and writing to TTS Cache while SQLite WAL writes are under heavy burst load. | `test_pair_02_tts_cache_and_database_wal_burst` (15 cache workers + 15 database workers, not 50 threads) |
| `T3_PAIR_03` | F1 Log Masking | F2 Error Responses | Upstream API failure with secret key; verify both HTTP response body and log records mask all tokens without double-encoding. | `test_pair_03_log_masking_and_http_error_response` |
| `T3_PAIR_04` | F7 Path Traversal | F5 TTS Cache | Malicious cache key containing `../../` attempted; verify sanitized into canonical SHA256 hex before filesystem stat. | `test_pair_04_path_traversal_and_tts_cache` |
| `T3_PAIR_05` | F3 System Telemetry | F6 Database WAL | System status requested simultaneously during a heavy database checkpoint operation; verify zero lock contention. | Not implemented (no test pairs `/api/system/status` with a WAL checkpoint) |
| `T3_PAIR_06` | F9 Telegram Bot | F4 Memory & Affection | Telegram user changes nicknames and triggers memory extraction; verify affection levels and nicknames update atomically in SQLite. | `test_pair_05_telegram_bot_and_memory_affection` |
| `T3_PAIR_07` | F8 SSE Streaming | F5 TTS Cache | Real-time dialogue sentences hit pre-cached entries in memory; verify seamless stream synthesis interleaving. | Not implemented |
| `T3_PAIR_08` | F7 SSRF Guard | F2 Error Responses | SSRF attempt to `http://169.254.169.254/` blocked; verify HTTP 400 detail explains restriction without leaking internal networking. | Not implemented as a pair; the metadata-IP block is asserted alone in `test_f7_03_ssrf_url_guard_blocks_private_ranges` |
| `T3_PAIR_09` | F9 Telegram Bot | F1 Log Masking | Telegram bot polling fails with 401 Unauthorized; verify bot token in Telegram URL is masked in logger. | Not implemented as a pair; standalone token masking is asserted in `test_f1_04_masking_telegram_bot_token` |
| `T3_PAIR_10` | F5 TTS Cache | F7 Path Traversal | Audio cleanup worker scans `audio/` directory; verify `audio/cache/` is strictly protected while invalid traversal files are rejected. | Not implemented; the cleaner's database commit path alone is tested in `tests/test_stability_resilience_loop.py::test_audio_cleaner_db_deletion_committed` |

---

## 6. Tier 4: Real-World Application Scenarios

Tier 4 simulates realistic, end-to-end user workflows and real production workloads:

`TestTier4RealWorldApplicationScenarios` implements **4** scenario methods (`test_scenario_01`–`test_scenario_04`),
and their numbering does not follow the headings below: Scenarios 1, 2 and 3 map to `test_scenario_01`–`test_scenario_03`,
Scenario 5 maps to `test_scenario_04`, and Scenario 4 has no test. The bullet lines marked **Implemented as** state what
each method actually drives, because several workflow steps above them are specifications rather than assertions.

### Scenario 1: Multi-Turn Galgame Session with Evolving Memory & Affection
- **Workflow**:
  1. User introduces themselves: "你好，我叫翔太，是一名程序员。" -> Facts extracted: `player_name="翔太"`, `occupation="程序员"`.
  2. Character responds warmly, updating affection score from Level 1 to Level 2.
  3. User states preference: "我最喜欢吃拉面和甜甜圈。" -> Fact extracted: `like_拉面和甜甜圈`.
  4. Next turn: "今天下班好累啊。" -> System retrieves memories `翔太`, `程序员`, `拉面` and injects them into system prompt block.
  5. Character mentions eating ramen together after programming work.
- **Verification**: Database states, memory retrieval composite score, affection transitions, and SSE tokens.
- **Implemented as**: `test_scenario_01_full_galgame_dialogue_and_memory_lifecycle`. It introduces the user with
  "你可以叫我翔太就好！", states the preference "我最喜欢抹茶拿铁了。", recalls against "今天工作好忙，想喝点甜的休息一下。",
  then asserts `"翔太" in facts_recalled`, `"抹茶拿铁" in facts_recalled`, `aff_state["score"] >= 1`, and that both values
  appear in `format_memory_prompt_block`. None of the other details in the workflow above is asserted: no `程序员`
  occupation extraction, no `拉面`/`甜甜圈` preference, no Level 1→2 affection transition assertion, and no SSE token assertion.

### Scenario 2: High-Concurrency Burst Multi-Tenant Chat
- **Workflow**:
  1. 10 simultaneous virtual users start SSE streaming sessions across different session IDs.
  2. Synthesizer generates audio chunks in parallel with token streaming.
  3. Shared GPT-SoVITS mutex synchronizes inference while memory LRU cache serves repeated dialogue phrases in `< 0.05ms`.
- **Verification**: Zero deadlocks, zero dropped SSE connections, 100% response completion, total throughput stability.
- **Implemented as**: `test_scenario_02_high_concurrency_multi_tenant_streaming`. Ten coroutine sessions feed a
  two-field JSON fixture through `StreamingBilingualParser` in 2-character chunks and each write one row via
  `crud.add_message`; the test then asserts one stored message per session. No HTTP/SSE connection is opened, the
  synthesizer and GPT-SoVITS inference mutex are not involved, and no latency or throughput figure is measured.

### Scenario 3: Upstream Chaos & Graceful Degradation
- **Workflow**:
  1. LLM provider suddenly times out after 100ms.
  2. TTS backend returns 502 Bad Gateway.
  3. Telegram Bot loses internet connection.
- **Verification**: FastAPI emits structured fallback error events, log files record masked alerts, database transactions roll back cleanly, service remains 100% operational for subsequent requests.
- **Implemented as**: `test_scenario_03_upstream_network_fault_and_graceful_degradation`. The method builds the app,
  POSTs one payload to `/api/providers/test`, and asserts HTTP 200 with `success` in the JSON body. The three failures
  listed in the workflow above are never injected — there is no 100ms LLM timeout, no `502` from the TTS backend, no
  Telegram connection drop, no rollback assertion, and no follow-up request proving the service stays operational.

### Scenario 4: Dynamic Configuration & Model Hot-Reloading
- **Workflow**:
  1. Admin updates GPT-SoVITS endpoint and switches active voice profile via REST API.
  2. Admin enables Telegram proxy and updates bot credentials.
  3. Ongoing user chat sessions continue without interruption.
- **Verification**: Active voice profile switches with automatic rollback on error; Telegram bot restarts polling automatically with new credentials.
- **Not implemented**: `TestTier4RealWorldApplicationScenarios` contains no `test_scenario_*` method for this
  workflow. The pieces exist separately — the voice-profile switch and its rollback are asserted in
  `tests/test_voice_manager_m3.py` (`test_rollback_on_step2_sovits_failure`,
  `test_rollback_on_step3_refer_audio_failure`) and the real `/api/voice/switch` route is driven against the app in
  `tests/test_m3_resilience_regression.py` —
  but `_reload_telegram_if_needed` (`galgame2voice/routers/config.py`) has no test asserting a polling restart, and no
  scenario test drives endpoint change + profile switch + proxy credential update while chat sessions run.

### Scenario 5: Cache Saturation & Continuous Pruning Under Load
- **Workflow**:
  1. Cache directory is populated with 5,000 synthesized audio entries exceeding max configured capacity (1024MB).
  2. Background pruning triggers, calculating least-recently-used entries.
  3. Pruner deletes oldest files and purges SQLite metadata down to 80% watermark.
- **Verification**: High-priority frequent audio entries remain in memory/disk; total cache storage stays strictly within bounds.
- **Implemented as**: `test_scenario_04_cache_saturation_and_lru_pruning_lifecycle`. It writes **15** entries against
  `TtsCacheManager(max_cache_mb=1, max_entries=5)` — not 5,000 entries against the 1024MB default — waits for background
  pruning, and asserts only `stats["total_files"] <= 15`. No frequency-priority retention and no 80%-watermark bound are
  asserted by that method.

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
- **Real-Clock Timers, No Global Clock Mocking**: Microsecond benchmarking reads the wall clock with
  `time.perf_counter()` / `time.monotonic()` and asserts generous thresholds (for example
  `tests/test_adversarial_m3_challenger2.py:198` asserts `statistics.mean(latencies) < 50.0`), so those
  measurements are machine-load dependent and are explicitly NOT deterministic; no test freezes the clock
  suite-wide. The only clock patching is per-test `monkeypatch.setattr(time, "monotonic", ...)` in
  `tests/test_benchmark_resilience_r4.py:576` and `tests/test_m3_resilience_regression.py:114`.
