# Project: Galgame2Voice Commercial Refactoring & Polish

## Architecture
Galgame2Voice is a high-performance, lightweight Python/FastAPI companion extension patch for GPT-SoVITS.
- **Backend Core**: Python 3.10+ (FastAPI + Uvicorn + httpx AsyncClient + aiosqlite in WAL mode + python-telegram-bot 21+).
- **Audio & TTS Engine**: GPT-SoVITS local/remote API (`http://127.0.0.1:9880`), guarded by `asyncio.Lock` inference mutex with 3-step atomic model switching (`/set_gpt_weights`, `/set_sovits_weights`, `/set_refer_audio`) and auto-rollback.
- **Bilingual Streaming Pipeline**: Dual-queue producer-consumer architecture (`ChatService.stream_chat`), streaming Chinese text tokens immediately via SSE (`event: text`) while slicing Japanese sentences for asynchronous voice synthesis (`event: audio_chunk`).
- **Frontend Presentation Layer**: Lightweight native HTML5/CSS3/ES6 (zero heavy framework overhead), featuring Dual-Mode immersion (🌸 Galgame Visual Novel Mode vs 💬 Classic Chat Stream Mode), Web Audio API `StreamingAudioPlayer` with `AnalyserNode` real-time soundwave jumping, and Top Capsule Control Bar.
- **Lifecycle & Automation Suite**: Zero-friction Windows batch scripts (`启动.bat`, `停止.bat`, `start.bat`, `stop.bat`) with multi-drive GPT-SoVITS local environment auto-probing, silent background execution, `/api/health` polling, browser auto-launch, and `/F /T` process-tree termination for 100% GPU VRAM release.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | One-Click Startup Automation | `启动.bat` / `start.bat` auto-probes local GPT-SoVITS, launches services silently, polls `/api/health`, and auto-opens `http://127.0.0.1:8080/`. | M1 | ORIGINAL_REQUEST §R1 |
| 2 | One-Click Stop & VRAM Release | `停止.bat` / `stop.bat` executes `/F /T` tree-kill on ports 8080 & 9880, removes PID file, and guarantees 100% GPU VRAM release. | M1 | ORIGINAL_REQUEST §R1 |
| 3 | Visual Novel Immersion Mode | Frosted glass dialogue box, character nameplate, avatar breathing halo, typewriter effect, bilingual alignment. | M2 | ORIGINAL_REQUEST §R2 |
| 4 | Classic Chat Stream Mode | Pastel bubble cards, instant speech playback, conversation reset button. | M2 | ORIGINAL_REQUEST §R2 |
| 5 | Top Capsule Control Bar | Quick voice profile selector, live GPT-SoVITS latency lamp, volume slider, mute toggle, console link. | M2 | ORIGINAL_REQUEST §R2 |
| 6 | Web Audio API Frequency Visualizer | `AnalyserNode` real-time frequency spectrum sampling driving jumping soundwave bars in sync with character voice. | M2 | ORIGINAL_REQUEST §R2 |
| 7 | SSE Stream Protocol Alignment | Align `chat_client.js` with backend SSE events (`event: text`, `event: audio_chunk`, `event: done`). | M2 | Explorer 2 Survey |
| 8 | Test Asset Compatibility | Add `.chat-container` / `.app-container` alias classes in `style.css` for M4 test suite. | M2 | Explorer 2 Survey |
| 9 | 100% Automated Test Regression | 415+ tests across Milestones 1~7 and adversarial stress suites passing with 0 failures, 0 errors. | M3 | ORIGINAL_REQUEST §R3 |
| 10 | Forensic Integrity Verification | Forensic auditor verifies zero hardcoding, zero facade mocks, and 100% authentic implementations. | M4 | System Prompt |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | One-Click Automation Scripts | Root `启动.bat`, `停止.bat`, `start.bat`, `stop.bat`, GPT-SoVITS path detection, silent daemon, VRAM cleanup. | none | DONE |
| M2 | Commercial Immersion Frontend UI | Visual Novel mode, Chat Stream mode, Top Capsule bar, AnalyserNode visualizer, SSE stream parser, Master Audio download button. | M1 | DONE |
| M3 | Full Test Suite Hardening & 100% Pass | Full pytest execution (451+ tests), test runner verification, edge case regression hardening. | M2 | DONE |
| M4 | Forensic Integrity Victory Audit | Comprehensive static and runtime forensic audit verifying genuine implementation and zero cheating. | M3 | DONE |

## Interface Contracts
### Startup Scripts ↔ Backend Ports
- `启动.bat` probes TCP 9880; if inactive, scans `%GPT_SOVITS_DIR%`, `E:\GPT-SoVITS-v2pro-20250604`, `D:\...`, `C:\...` and launches `api_v2.py` in background.
- `启动.bat` launches `python -m uvicorn galgame2voice.main:app --port 8080` in background, polls `http://127.0.0.1:8080/api/health`, and invokes `start http://127.0.0.1:8080/`.
- `停止.bat` terminates all processes listening on `:8080` and `:9880` via `taskkill /f /t /pid` and deletes `galgame2voice.pid`.

### Backend SSE ↔ Frontend ChatClient
- Endpoint: `POST /api/chat/stream`
- Event `text`: `data: {"delta_chinese": "string", "full_chinese": "string"}`
- Event `audio_chunk`: `data: {"index": int, "audio_url": "string", "sentence": "string"}`
- Event `done`: `data: {"chinese": "string", "japanese": "string", "audio_url": "string", "chunks": [...]}`

### Web Audio API ↔ Visualizer
- `StreamingAudioPlayer`: Node graph `Source -> ChunkGain -> MasterGain -> AnalyserNode -> Destination`.
- `AnalyserNode`: `fftSize = 64`, `smoothingTimeConstant = 0.8`.
- `getFrequencyData()`: Returns `Uint8Array` of frequency amplitudes to scale equalizer bars `scaleY(0.2 ~ 1.0)`.

## Code Layout
```
galgame2voice/
├── 启动.bat                        # Root Chinese one-click launcher
├── 停止.bat                        # Root Chinese one-click stop script
├── start.bat                       # Wrapper invoking 启动.bat
├── stop.bat                        # Wrapper invoking 停止.bat
├── start-galgame2voice.bat         # Developer wrapper
├── stop-galgame2voice.bat          # Developer wrapper
├── restart-galgame2voice.bat       # Developer wrapper
├── scripts/
│   ├── start-galgame2voice.bat     # English developer startup script
│   ├── stop-galgame2voice.bat      # English developer stop script
│   └── restart-galgame2voice.bat   # English developer restart script
├── galgame2voice/
│   ├── main.py                     # FastAPI app factory, lifespan, routing, static mounts
│   ├── config.py                   # Pydantic v2 Settings
│   ├── database/                   # SQLite schema, migrations, CRUD (WAL mode)
│   ├── adapters/                   # LLM & STT Provider Adapter Layer
│   ├── routers/                    # health, config, voice, chat routers
│   ├── services/                   # chat_service, gpt_sovits_client, voice_manager, session_manager
│   ├── static/                     # Web UI
│   │   ├── index.html              # Dual-mode stage, Top capsule bar, Dialogue box
│   │   ├── settings.html           # Web management console
│   │   ├── css/style.css           # Design system, glassmorphism, animations
│   │   └── js/
│   │       ├── audio_player.js     # Web Audio API player with AnalyserNode
│   │       └── chat_client.js      # ChatClient, SSE parser, view mode switcher
│   └── telegram_bot/               # Telegram bot handlers & async task queue
└── tests/                          # 415+ automated test cases
```
