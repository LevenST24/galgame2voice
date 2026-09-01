"""
Comprehensive Test Fixtures and Mocks for galgame2voice E2E Test Suite.
Includes Mock GPT-SoVITS Server, Mock LLM/STT Providers, In-Memory SQLite DB, and Test Clients.
"""

import asyncio
import json
import os
import sqlite3
import tempfile
from typing import AsyncGenerator, Dict, Any, List, Optional
import pytest
import httpx


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
        user_prompt = ""
        if messages:
            user_prompt = messages[-1].get("content", "")

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
    telegram_admin_ids TEXT NOT NULL DEFAULT '',
    allow_private_llm_endpoints INTEGER NOT NULL DEFAULT 0,
    console_token TEXT NOT NULL DEFAULT 'test_console_token',
    console_url TEXT NOT NULL DEFAULT '',
    max_history_messages INTEGER NOT NULL DEFAULT 10,
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
def mock_llm_server():
    """Provides a fresh instance of MockLLMServer."""
    return MockLLMServer()


@pytest.fixture(autouse=True)
def isolate_test_database(monkeypatch):
    """Ensures every test runs against an isolated temporary SQLite database."""
    # The test suite drives the app through raw ASGI clients without console
    # tokens; dedicated auth tests re-enable authentication themselves.
    monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "1")
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_auto_iso_")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(DATABASE_SCHEMA_SQL)
    conn.commit()
    conn.close()
    
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", path)
    monkeypatch.setenv("GALGAME_DB_PATH", path)
    from galgame2voice.routers.chat import set_chat_service
    set_chat_service(None)
    yield path
    set_chat_service(None)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


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
