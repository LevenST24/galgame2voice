"""
Comprehensive Test Fixtures and Mocks for galgame2voice E2E Test Suite.
Includes Mock GPT-SoVITS Server, Mock LLM/STT Providers, In-Memory SQLite DB, and Test Clients.
"""

import asyncio
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
from typing import AsyncGenerator, Dict, Any, List, Optional
import pytest
import httpx

from galgame2voice.security import url_guard
from galgame2voice.security.url_guard import OFFICIAL_LLM_HOSTS

# 净化沙箱环境中 no_proxy/NO_PROXY 环境变量里的带方括号 IPv6 地址（如 [::1]、[fd8b:4f84:7d32:99::1]）。
# 沙箱环境注入的代理忽略列表中常包含带括号的 IPv6 地址，但 httpx 创建 Client 时通过 urlparse 解析代理规则，
# 会将形如 [::1] 误判为 host 为 "["、port 为 ":1]"，进而抛出 httpx.InvalidURL: Invalid port: ':1]'。
# 将其净化为无括号形式（::1）后，httpx 能正常解析，且不破坏原有的代理绕过名单。
for _proxy_var in ("no_proxy", "NO_PROXY"):
    _val = os.environ.get(_proxy_var)
    if _val:
        os.environ[_proxy_var] = re.sub(r"\[([0-9a-fA-F:]+)\]", r"\1", _val)

os.environ.setdefault("GALGAME2VOICE_SKIP_MEM_CHECK", "1")
# The suite drives the ASGI app in-process (Host: "test"), which the DNS-rebinding
# Host allowlist would reject; the middleware has its own dedicated test that opts
# back in explicitly.
os.environ.setdefault("GALGAME2VOICE_HOST_HEADER_VALIDATION_DISABLED", "1")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "requires_character_assets: mark test as requiring installed character assets in characters/",
    )
    for line in _CAPABILITY_MARKER_DESCRIPTIONS:
        config.addinivalue_line("markers", line)


# ---------------------------------------------------------------------------
# Host capability probes
# ---------------------------------------------------------------------------
# A few tests genuinely need OS facilities that locked-down hosts withhold: a
# private temp directory the process can actually write into, and subprocesses
# with piped stdio (on Windows those need a named pipe, which some sandboxes and
# endpoint-security products refuse to create). On such a host those tests used
# to fail with a bare PermissionError, which reads like a product regression.
#
# They are marked instead, and the marker is turned into a skip with an explicit
# reason only when the capability is really missing. Each probe performs the
# operation it claims to test, so a host that can do the work never skips.

_CAPABILITY_MARKER_DESCRIPTIONS = (
    "requires_piped_subprocess: needs subprocesses with piped stdio (real ffmpeg / audio conversion)",
    "requires_writable_temp_dir: needs a private temp directory the process can write into",
    "requires_process_termination: needs permission to terminate a child process it started",
    "wall_clock: asserts an absolute wall-clock budget, so it is skipped under coverage instrumentation",
)

_PROBE_SUBPROCESS_TIMEOUT_SECONDS = 60.0

WRITABLE_TEMP_DIR_SKIP_REASON = (
    "host does not provide a private temp directory the process can write into "
    "(tempfile.mkdtemp() returns an unusable directory here)"
)
PIPED_SUBPROCESS_SKIP_REASON = (
    "host cannot spawn subprocesses with piped stdio, which on Windows requires "
    "a named pipe (real ffmpeg/audio-conversion paths cannot run here)"
)
PROCESS_TERMINATION_SKIP_REASON = (
    "host denies terminating a process this suite started, so process-tree "
    "cleanup cannot be exercised here"
)


def _probe_writable_private_temp_dir() -> bool:
    """True when a tempfile.mkdtemp() directory can be written into."""
    try:
        path = tempfile.mkdtemp(prefix="g2v_cap_")
    except OSError:
        return False
    try:
        with open(os.path.join(path, "probe.txt"), "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.makedirs(os.path.join(path, "sub"), exist_ok=True)
        return True
    except OSError:
        return False


def _probe_piped_subprocess() -> bool:
    """True when asyncio can spawn a subprocess with piped stdio."""
    async def _run() -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "print('ok')",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, NotImplementedError):
            return False
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_PROBE_SUBPROCESS_TIMEOUT_SECONDS
            )
        except (OSError, asyncio.TimeoutError):
            proc.kill()
            return False
        return proc.returncode == 0 and b"ok" in (stdout or b"")

    probe = _run()
    try:
        return asyncio.run(probe)
    except Exception:
        # A secondary import of conftest from an async test can happen while a
        # loop is running. asyncio.run then rejects the still-unstarted probe.
        probe.close()
        return False


def _probe_process_termination() -> bool:
    """True when the host lets us terminate a process this suite started."""
    try:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    except OSError:
        return False
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["taskkill", "/f", "/pid", str(proc.pid)],
                capture_output=True,
                text=True,
            )
            return result.returncode == 0
        proc.terminate()
        return True
    except OSError:
        return False
    finally:
        try:
            proc.kill()
        except OSError:
            pass


HOST_SUPPORTS_WRITABLE_TEMP_DIR = _probe_writable_private_temp_dir()
HOST_SUPPORTS_PIPED_SUBPROCESS = _probe_piped_subprocess()
HOST_SUPPORTS_PROCESS_TERMINATION = _probe_process_termination()


def pytest_collection_modifyitems(config, items):
    """Turns capability markers into skips when the host lacks the capability."""
    # A wall-clock assertion and a coverage run cannot both be honest: tracing
    # multiplies the cost of every executed line, so a budget like "average cache
    # hit under 0.005 ms" measures the tracer, not the cache. Skipping is the
    # only outcome that does not turn the coverage job into a false alarm.
    coverage_active = config.getoption("cov_source", None) is not None
    if coverage_active:
        for item in items:
            if "wall_clock" in item.keywords:
                item.add_marker(pytest.mark.skip(
                    reason="wall-clock assertion is meaningless under coverage instrumentation"
                ))

    if not HOST_SUPPORTS_PIPED_SUBPROCESS:
        for item in items:
            if "requires_piped_subprocess" in item.keywords:
                item.add_marker(pytest.mark.skip(reason=PIPED_SUBPROCESS_SKIP_REASON))
    if not HOST_SUPPORTS_WRITABLE_TEMP_DIR:
        for item in items:
            if "requires_writable_temp_dir" in item.keywords:
                item.add_marker(pytest.mark.skip(reason=WRITABLE_TEMP_DIR_SKIP_REASON))
    if not HOST_SUPPORTS_PROCESS_TERMINATION:
        for item in items:
            if "requires_process_termination" in item.keywords:
                item.add_marker(pytest.mark.skip(reason=PROCESS_TERMINATION_SKIP_REASON))



def _filter_aiosqlite_teardown_race(args: threading.ExceptHookArgs) -> None:
    """Swallows the known aiosqlite worker-thread race at loop teardown.

    When pytest-asyncio closes a test's event loop while a fire-and-forget
    background task's aiosqlite connection is still finishing, the worker
    thread raises RuntimeError('Event loop is closed'). This is harmless
    teardown noise, not a product bug; any other thread exception still
    surfaces through pytest's threadexception plugin.
    """
    exc = args.exc_value
    is_aiosqlite_worker = False
    tb = args.exc_traceback
    while tb is not None:
        if "aiosqlite" in tb.tb_frame.f_code.co_filename:
            is_aiosqlite_worker = True
            break
        tb = tb.tb_next
    if isinstance(exc, RuntimeError) and str(exc) == "Event loop is closed" and is_aiosqlite_worker:
        return
    _original_thread_excepthook(args)


_original_thread_excepthook = threading.excepthook
threading.excepthook = _filter_aiosqlite_teardown_race


def mask_secret(secret: str) -> str:
    """Helper to mask API keys and bot tokens (e.g. sk-****cdef)."""
    if not secret:
        return ""
    if len(secret) <= 8:
        return "********"
    if secret.startswith("sk-") and len(secret) > 8:
        return f"sk-****{secret[-4:]}"
    prefix = secret[:3]
    suffix = secret[-4:]
    return f"{prefix}****{suffix}"


# ============================================================================
# 1. Mock GPT-SoVITS Backend Simulator
# ============================================================================

class MockGptSovitsServer:
    """
    Simulates GPT-SoVITS FastAPI/Uvicorn backend (default port 9880).
    Implements /set_gpt_weights, /set_sovits_weights, /set_refer_audio, /tts, and health check.
    Supports simulated network errors, latency, and step-specific rollback triggers.
    """
    def __init__(self, base_url: str = "http://127.0.0.1:9880"):
        self.base_url = base_url
        self.is_online: bool = True
        self.current_gpt_weights: str = "weights/default.ckpt"
        self.current_sovits_weights: str = "weights/default.pth"
        self.current_refer_audio: str = "ref/default.wav"
        self.current_refer_text: str = "デフォルトの音声テキストです。"
        self.current_refer_language: str = "ja"
        
        # Diagnostics & Call Tracking
        self.call_history: List[Dict[str, Any]] = []
        self.failure_step: Optional[str] = None
        self.simulate_latency_s: float = 0.0
        self.concurrent_requests_count: int = 0
        self.max_concurrent_seen: int = 0

    def record_call(self, endpoint: str, payload: Dict[str, Any]):
        self.call_history.append({"endpoint": endpoint, "payload": payload})

    def fail_on_step(self, step_name: Optional[str]):
        """Forces failure on a specific step: 'set_gpt_weights', 'set_sovits_weights', 'set_refer_audio', 'tts'"""
        self.failure_step = step_name

    def set_online(self, status: bool):
        self.is_online = status

    async def handle_request(self, method: str, path: str, json_data: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
        if not self.is_online:
            return httpx.Response(status_code=503, json={"error": "GPT-SoVITS backend service is offline"})

        if self.simulate_latency_s > 0:
            await asyncio.sleep(self.simulate_latency_s)

        self.concurrent_requests_count += 1
        self.max_concurrent_seen = max(self.max_concurrent_seen, self.concurrent_requests_count)

        try:
            self.record_call(path, json_data or params or {})

            if path in ("/control", "/health", "/"):
                return httpx.Response(status_code=200, json={"status": "running", "version": "v2", "service": "GPT-SoVITS"})

            if path == "/set_gpt_weights":
                if self.failure_step == "set_gpt_weights":
                    return httpx.Response(status_code=500, json={"code": 1, "message": "Mocked GPT weights load error"})
                weights_path = (json_data or {}).get("weights_path") or (params or {}).get("weights_path")
                if not weights_path or "invalid" in str(weights_path):
                    return httpx.Response(status_code=400, json={"code": 1, "message": "Invalid GPT weights path"})
                self.current_gpt_weights = str(weights_path)
                return httpx.Response(status_code=200, json={"code": 0, "message": "GPT weights updated successfully"})

            elif path == "/set_sovits_weights":
                if self.failure_step == "set_sovits_weights":
                    return httpx.Response(status_code=500, json={"code": 1, "message": "Mocked SoVITS weights load error"})
                weights_path = (json_data or {}).get("weights_path") or (params or {}).get("weights_path")
                if not weights_path or "invalid" in str(weights_path):
                    return httpx.Response(status_code=400, json={"code": 1, "message": "Invalid SoVITS weights path"})
                self.current_sovits_weights = str(weights_path)
                return httpx.Response(status_code=200, json={"code": 0, "message": "SoVITS weights updated successfully"})

            elif path == "/set_refer_audio":
                if self.failure_step == "set_refer_audio":
                    return httpx.Response(status_code=500, json={"code": 1, "message": "Mocked Refer Audio load error"})
                data = json_data or params or {}
                refer_path = data.get("refer_audio_path")
                if not refer_path or "invalid" in str(refer_path):
                    return httpx.Response(status_code=400, json={"code": 1, "message": "Invalid refer audio path"})
                self.current_refer_audio = str(refer_path)
                self.current_refer_text = data.get("refer_text", "")
                self.current_refer_language = data.get("refer_language", "ja")
                return httpx.Response(status_code=200, json={"code": 0, "message": "Refer audio updated successfully"})

            elif path == "/tts":
                if self.failure_step == "tts":
                    return httpx.Response(status_code=500, json={"code": 1, "message": "Mocked TTS synthesis failed"})
                data = json_data or params or {}
                text = data.get("text", "")
                if not text:
                    return httpx.Response(status_code=400, json={"code": 1, "message": "Empty text for TTS"})
                
                # Generate synthetic WAV header and PCM audio bytes
                mock_wav_header = b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00"
                mock_audio_bytes = mock_wav_header + (b"\x00\x7f" * 1024)
                return httpx.Response(
                    status_code=200,
                    content=mock_audio_bytes,
                    headers={"Content-Type": "audio/wav"}
                )

            return httpx.Response(status_code=404, json={"error": f"Endpoint {path} not found"})
        finally:
            self.concurrent_requests_count -= 1


# ============================================================================
# 2. Mock LLM and STT Provider Server
# ============================================================================

class MockLLMServer:
    """
    Simulates OpenAI-compatible REST API (/v1/chat/completions, /v1/models, /v1/audio/transcriptions).
    Supports streaming Server-Sent Events, bilingual output generation, and error modes.
    """
    def __init__(self, api_key: str = "sk-test-mock-key-12345"):
        self.api_key = api_key
        self.simulated_models = ["gpt-4o", "gpt-4o-mini", "deepseek-chat", "qwen-max", "glm-4-flash"]
        self.force_error_code: Optional[int] = None
        self.force_error_message: str = "Mocked LLM API Error"
        self.default_chinese_reply: str = "你好！很高兴见到你，今天想聊些什么呢？"
        self.default_japanese_reply: str = "こんにちは！お会いできて嬉しいです、今日は何をお話ししましょうか？"

    def set_error(self, code: Optional[int], message: str = "Mocked LLM API Error"):
        self.force_error_code = code
        self.force_error_message = message

    def generate_bilingual_json(self, chinese: Optional[str] = None, japanese: Optional[str] = None) -> str:
        payload = {
            "chinese": chinese or self.default_chinese_reply,
            "japanese": japanese or self.default_japanese_reply
        }
        return json.dumps(payload, ensure_ascii=False)

    def generate_streaming_chunks(self, full_text: str) -> List[str]:
        """Splits full_text into small incremental token chunks formatted as SSE data: lines"""
        chunks = []
        chunk_size = max(1, len(full_text) // 10)
        for i in range(0, len(full_text), chunk_size):
            token = full_text[i:i + chunk_size]
            sse_obj = {
                "id": "chatcmpl-mock-123",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": "gpt-4o",
                "choices": [{
                    "index": 0,
                    "delta": {"content": token},
                    "finish_reason": None
                }]
            }
            chunks.append(f"data: {json.dumps(sse_obj, ensure_ascii=False)}\n\n")
        
        # Terminal chunk
        done_obj = {
            "id": "chatcmpl-mock-123",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        }
        chunks.append(f"data: {json.dumps(done_obj)}\n\n")
        chunks.append("data: [DONE]\n\n")
        return chunks

    async def handle_chat_completion(self, json_data: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> httpx.Response:
        auth_header = (headers or {}).get("authorization", "")
        if not auth_header or not auth_header.startswith("Bearer "):
            return httpx.Response(status_code=401, json={"error": {"message": "Invalid or missing API key", "type": "invalid_request_error"}})

        if self.force_error_code:
            return httpx.Response(status_code=self.force_error_code, json={"error": {"message": self.force_error_message}})

        messages = json_data.get("messages", [])
        is_stream = json_data.get("stream", False)
        model = json_data.get("model", "gpt-4o")

        # Check last message content
        if messages:
            messages[-1].get("content", "")

        json_content = self.generate_bilingual_json()

        if is_stream:
            chunks = self.generate_streaming_chunks(json_content)
            async def event_generator():
                for c in chunks:
                    yield c.encode("utf-8")
            return httpx.Response(
                status_code=200,
                headers={"Content-Type": "text/event-stream"},
                content="".join(chunks).encode("utf-8")
            )

        return httpx.Response(
            status_code=200,
            json={
                "id": "chatcmpl-mock-sync-123",
                "object": "chat.completion",
                "created": 1700000000,
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json_content
                    },
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 50, "completion_tokens": 60, "total_tokens": 110}
            }
        )

    async def handle_models_list(self) -> httpx.Response:
        if self.force_error_code:
            return httpx.Response(status_code=self.force_error_code, json={"error": {"message": self.force_error_message}})
        data = [{"id": m, "object": "model", "owned_by": "mock"} for m in self.simulated_models]
        return httpx.Response(status_code=200, json={"object": "list", "data": data})

    async def handle_transcription(self, files: Any = None, data: Any = None) -> httpx.Response:
        if self.force_error_code:
            return httpx.Response(status_code=self.force_error_code, json={"error": {"message": self.force_error_message}})
        return httpx.Response(status_code=200, json={"text": "おはようございます。今日も一日頑張りましょう！"})


# ============================================================================
# 3. In-Memory / Temporary SQLite Database Fixtures & Schema Helper
# ============================================================================

DATABASE_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE DEFAULT NULL,
    value TEXT DEFAULT NULL,
    description TEXT DEFAULT '',
    active_provider_id TEXT NOT NULL DEFAULT 'gemini',
    active_voice_profile_id INTEGER DEFAULT 1,
    gpt_sovits_url TEXT NOT NULL DEFAULT 'http://127.0.0.1:9880',
    audio_output_dir TEXT NOT NULL DEFAULT 'audio',
    audio_retention_minutes INTEGER NOT NULL DEFAULT 30,
    audio_cleanup_interval_sec INTEGER NOT NULL DEFAULT 600,
    speed_factor REAL NOT NULL DEFAULT 1.0,
    temperature REAL NOT NULL DEFAULT 1.0,
    top_k INTEGER NOT NULL DEFAULT 15,
    top_p REAL NOT NULL DEFAULT 1.0,
    seed INTEGER NOT NULL DEFAULT -1,
    batch_size INTEGER NOT NULL DEFAULT 1,
    text_split_method TEXT NOT NULL DEFAULT 'cut1',
    fragment_interval REAL NOT NULL DEFAULT 0.3,
    telegram_bot_token TEXT NOT NULL DEFAULT '',
    telegram_bot_username TEXT NOT NULL DEFAULT 'natsume_siki_bot',
    telegram_proxy_host TEXT NOT NULL DEFAULT '127.0.0.1',
    telegram_proxy_port INTEGER NOT NULL DEFAULT 10809,
    telegram_proxy_enabled INTEGER NOT NULL DEFAULT 0,
    telegram_enabled INTEGER NOT NULL DEFAULT 0,
    telegram_admin_ids TEXT NOT NULL DEFAULT '',
    allow_private_llm_endpoints INTEGER NOT NULL DEFAULT 0,
    console_token TEXT NOT NULL DEFAULT 'test_console_token',
    console_url TEXT NOT NULL DEFAULT '',
    max_history_messages INTEGER NOT NULL DEFAULT 10,
    inference_precision TEXT NOT NULL DEFAULT 'auto',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    provider_type TEXT NOT NULL DEFAULT '',
    api_base_url TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    chat_model TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    stt_model TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 0,
    custom_headers TEXT NOT NULL DEFAULT '{}',
    extra_config TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_providers_is_active ON providers(is_active);

CREATE TABLE IF NOT EXISTS voice_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    gpt_weights_path TEXT NOT NULL,
    sovits_weights_path TEXT NOT NULL,
    ref_audio_path TEXT NOT NULL DEFAULT '',
    refer_audio_path TEXT NOT NULL DEFAULT '',
    prompt_text TEXT NOT NULL DEFAULT '',
    refer_text TEXT NOT NULL DEFAULT '',
    prompt_lang TEXT NOT NULL DEFAULT 'ja',
    refer_language TEXT NOT NULL DEFAULT 'ja',
    text_lang TEXT NOT NULL DEFAULT 'ja',
    prompt_language TEXT NOT NULL DEFAULT 'ja',
    text_language TEXT NOT NULL DEFAULT 'ja',
    system_prompt TEXT NOT NULL DEFAULT '',
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_voice_profiles_is_default ON voice_profiles(is_default);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL DEFAULT 'web',
    user_id TEXT NOT NULL DEFAULT '',
    voice_profile_id INTEGER REFERENCES voice_profiles(id) ON DELETE SET NULL,
    custom_system_prompt TEXT DEFAULT NULL,
    token_budget INTEGER NOT NULL DEFAULT 4096,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sessions_channel ON sessions(channel);
CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_voice_profile ON sessions(voice_profile_id);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content_chinese TEXT NOT NULL,
    content_japanese TEXT NOT NULL DEFAULT '',
    audio_url TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_messages_session_created ON messages(session_id, created_at);

CREATE TABLE IF NOT EXISTS session_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content_chinese TEXT,
    content_japanese TEXT,
    raw_content TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_session_id ON session_messages(session_id);

CREATE TABLE IF NOT EXISTS user_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'default_user',
    character_id INTEGER DEFAULT 1,
    category TEXT NOT NULL DEFAULT 'preference',
    fact_key TEXT NOT NULL,
    fact_value TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    source_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    recall_count INTEGER NOT NULL DEFAULT 0,
    last_recalled_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_user_memories_user_cat ON user_memories(user_id, category);
CREATE INDEX IF NOT EXISTS idx_user_memories_char_key ON user_memories(character_id, fact_key);
CREATE UNIQUE INDEX IF NOT EXISTS ux_user_memories_key ON user_memories(user_id, character_id, fact_key);

CREATE TABLE IF NOT EXISTS character_affection (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'default_user',
    character_id INTEGER NOT NULL DEFAULT 1,
    affection_score INTEGER NOT NULL DEFAULT 0,
    affection_level INTEGER NOT NULL DEFAULT 1,
    current_emotion TEXT NOT NULL DEFAULT 'normal',
    interaction_count INTEGER NOT NULL DEFAULT 0,
    daily_points_earned INTEGER NOT NULL DEFAULT 0,
    last_interaction_date TEXT NOT NULL DEFAULT '',
    unlocked_dialogues TEXT NOT NULL DEFAULT '[]',
    custom_nickname TEXT DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, character_id)
);
CREATE INDEX IF NOT EXISTS idx_affection_user_char ON character_affection(user_id, character_id);
"""


@pytest.fixture
def mock_gpt_sovits():
    """Provides a fresh instance of MockGptSovitsServer."""
    return MockGptSovitsServer()


@pytest.fixture
async def configured_voice(tmp_path):
    """Supply an explicit neutral voice for tests that exercise audio output."""
    import wave
    from galgame2voice.database import crud
    from galgame2voice.database.models import VoiceProfileCreate
    from galgame2voice.database.session import get_db, init_db
    await init_db()
    reference = tmp_path / "test_reference.wav"
    with wave.open(str(reference), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 16000 * 4)
    async with get_db() as conn:
        profile = await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="Test voice", gpt_weights_path="test.ckpt", sovits_weights_path="test.pth",
            ref_audio_path=str(reference), prompt_text="Test reference", prompt_lang="ja",
        ))
        assert await crud.set_active_voice_profile(conn, profile.id)
    return profile


@pytest.fixture
def mock_llm_server():
    """Provides a fresh instance of MockLLMServer."""
    return MockLLMServer()


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Drops the lru_cached Settings around every test.

    ``get_settings()`` is memoized, so a test that redirects configuration
    through the environment (project root, data dir, DB path) would otherwise
    leave the next test reading the previous test's resolved settings — or read
    a stale object resolved before its own ``monkeypatch.setenv`` ran.
    """
    from galgame2voice.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def forbid_unmarked_real_ffmpeg(request, monkeypatch):
    """Unit tests must not reach the real ffmpeg executable.

    ``is_ffmpeg_available()`` is **not** the decision point the conversion path
    uses — that is ``_run_ffmpeg_transcode`` → ``_require_ffmpeg_bin`` →
    ``find_ffmpeg``. A test that patches ``is_ffmpeg_available()`` therefore only
    *believes* it mocked ffmpeg: the real binary is still discovered, and the
    test quietly depends on whether the host happens to ship ffmpeg. That is how
    a suite goes red on a runner while passing on the developer's machine.

    Fail loudly instead. Tests that genuinely exercise the toolchain declare
    ``requires_piped_subprocess`` and are exempt.
    """
    if request.node.get_closest_marker("requires_piped_subprocess"):
        return

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "unit test reached real ffmpeg discovery through find_ffmpeg(); patch "
            "galgame2voice.utils.audio_converter.find_ffmpeg (the real decision "
            "point) or mark the test requires_piped_subprocess if it genuinely "
            "runs the toolchain"
        )

    monkeypatch.setattr("galgame2voice.utils.audio_converter.find_ffmpeg", _forbidden)


@pytest.fixture(autouse=True)
def isolate_test_database(monkeypatch, tmp_path):
    """Ensures every test runs against an isolated temporary SQLite database."""
    # The test suite drives the app through raw ASGI clients without console
    # tokens; dedicated auth tests re-enable authentication themselves.
    monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "1")
    # Redirect credential files as well as SQLite: ignored data/ is user data.
    test_data_dir = tmp_path / ".runtime-data"
    test_data_dir.mkdir()
    monkeypatch.setenv("DATA_DIR_NAME", str(test_data_dir))
    monkeypatch.setenv("AUDIO_DIR_NAME", str(tmp_path / ".runtime-audio"))
    monkeypatch.setenv("LOGS_DIR_NAME", str(tmp_path / ".runtime-logs"))
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    # Match Settings.db_path as well as the legacy DB environment override.
    path = str(test_data_dir / "galgame2voice.db")
    conn = sqlite3.connect(path)
    conn.executescript(DATABASE_SCHEMA_SQL)
    conn.commit()
    conn.close()
    
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", path)
    monkeypatch.setenv("GALGAME_DB_PATH", path)
    from galgame2voice.config import get_settings
    get_settings.cache_clear()
    from galgame2voice.routers.chat import set_chat_service
    set_chat_service(None)
    yield path
    set_chat_service(None)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@pytest.fixture(autouse=True)
async def isolate_runtime_services(monkeypatch, isolate_test_database):
    """Singletons must use this test's paths and release their background jobs."""
    from galgame2voice.services import gpt_sovits_client, tts_cache_manager, tts_scheduler, voice_manager, voice_resolver
    monkeypatch.setattr(tts_cache_manager, "_tts_cache_manager_instance", None)
    monkeypatch.setattr(tts_scheduler, "_GLOBAL_TTS_SCHEDULER", None)
    monkeypatch.setattr(voice_manager, "_global_voice_manager", None)
    monkeypatch.setattr(gpt_sovits_client, "_global_gpt_sovits_client", None)
    monkeypatch.setattr(voice_resolver, "_GLOBAL_VOICE_RESOLVER", None)
    yield
    scheduler = tts_scheduler._GLOBAL_TTS_SCHEDULER
    if isinstance(scheduler, tts_scheduler.TtsScheduler):
        await scheduler.aclose()
    manager = voice_manager._global_voice_manager
    if isinstance(manager, voice_manager.VoiceManager):
        await manager.aclose()
    cache = tts_cache_manager._tts_cache_manager_instance
    if isinstance(cache, tts_cache_manager.TtsCacheManager):
        await cache.aclose()
    await gpt_sovits_client.close_gpt_sovits_client()


@pytest.fixture(autouse=True)
def hermetic_dns_for_official_hosts(monkeypatch):
    """实现测试环境的 DNS hermetic 化，避免沙箱环境不可信 DNS 影响测试。

    说明与理由：
    1. 这些测试（如各类 adapter 和 provider 测试）mock 了 HTTP transport，本意只测重试与诊断逻辑；
    2. 官方 host 本就是 allowlist 设计（url_guard.py 第 134 行对官方 host 强制 https 即体现了这一点）；
    3. 沙箱 DNS 不可信（官方域名常被解析到私网 IP 导致 _resolve_host 做真实 DNS 检查时抛 PermissionError 或返回拒绝误杀测试）。

    对 host.lower() 在 OFFICIAL_LLM_HOSTS 中的官方 host 直接返回 (True, "")，不做真实 DNS 解析；
    否则调用原始 _resolve_host(host, port)。
    """
    orig_resolve_host = url_guard._resolve_host

    def wrapper(host: str, port: int):
        hl = host.lower()
        if hl in OFFICIAL_LLM_HOSTS:
            return True, ""
        # RFC 保留域名与单测占位域名直通：
        # 理由：
        # 1. RFC 2606 / RFC 6761 保留的文档/测试专用域名（example.com, example.org, example.net 及 .test, .invalid, .example 后缀）；
        # 2. 测试套件内实际使用的占位域名（api.test.com、api.special.org，见 test_adversarial_m5.py、test_settings_console.py）。
        # 永远不可能是真实 SSRF 目标；单测使用它们做占位 URL 只关心 CRUD、表单回显与重试/错误诊断逻辑。
        # 沙箱环境不可信 DNS 会将任意域名解析到私网 IP 导致误杀，故在此直通避免沙箱污染。
        if (
            hl in ("example.com", "example.org", "example.net")
            or hl.endswith(
                (
                    ".test",
                    ".invalid",
                    ".example",
                    ".example.com",
                    ".example.org",
                    ".example.net",
                )
            )
            or hl in ("api.test.com", "api.special.org")
        ):
            return True, ""
        return orig_resolve_host(host, port)

    # 保留被替换函数的 cache_clear 属性（url_guard.py 第 111 行给 _resolve_host 挂了 cache_clear = clear_dns_cache），
    # wrapper 上也要能调到，否则引用它的测试会坏。
    wrapper.cache_clear = getattr(orig_resolve_host, "cache_clear", url_guard.clear_dns_cache)

    monkeypatch.setattr("galgame2voice.security.url_guard._resolve_host", wrapper)


@pytest.fixture(autouse=True)
def require_character_assets_guard(request):
    """当测试带有 requires_character_assets marker 且 characters/ 下没有任何有效角色包时自动 skip。"""
    marker = request.node.get_closest_marker("requires_character_assets")
    if marker is not None:
        characters_dir = Path(__file__).resolve().parent.parent / "characters"
        has_valid_package = (
            characters_dir.is_dir()
            and any(
                (subdir / "manifest.json").is_file()
                for subdir in characters_dir.iterdir()
                if subdir.is_dir()
            )
        )
        if not has_valid_package:
            pytest.skip(
                "character asset packages not installed (characters/ is empty; git-ignored user assets)"
            )


@pytest.fixture
def temp_db_path():
    """Creates a temporary sqlite database file initialized with the full schema."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_galgame2voice_")
    os.close(fd)
    
    conn = sqlite3.connect(path)
    conn.executescript(DATABASE_SCHEMA_SQL)
    conn.commit()
    conn.close()

    yield path

    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@pytest.fixture
def sample_voice_profile():
    return {
        "name": "Arona",
        "gpt_weights_path": "weights/arona-e15.ckpt",
        "sovits_weights_path": "weights/arona_e24_s1200.pth",
        "refer_audio_path": "ref/arona_greeting.wav",
        "refer_text": "先生、今日もよろしくお願いしますね！",
        "refer_language": "ja",
        "prompt_language": "ja",
        "text_language": "ja",
        "is_default": 1
    }


@pytest.fixture
def sample_provider_config():
    return {
        "id": "openai_primary",
        "provider_type": "openai",
        "api_key": "sk-1234567890abcdef1234567890abcdef",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "is_active": 1,
        "extra_config": json.dumps({"temperature": 0.8, "max_tokens": 1024})
    }


@pytest.fixture
def sample_streaming_chunks():
    """Returns sample token chunks of a bilingual JSON LLM response."""
    return [
        '{"chinese": "',
        '你好，',
        '指挥官！',
        '今天的天气',
        '很适合出海呢。',
        '", "japanese": "',
        'こんにちは、',
        '指揮官！',
        '今日の天気は',
        '出海にぴったりですね。',
        '"}'
    ]
