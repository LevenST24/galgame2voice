"""
Asynchronous CRUD operations for SQLite persistence in galgame2voice.
Handles schema migrations, seed initializations, data queries, and key masking.
"""

import json
import sqlite3
import uuid
from typing import List, Optional, Dict, Any, Tuple
import aiosqlite

from galgame2voice.database.models import (
    SettingsInDB, SettingsResponse, SettingsUpdate,
    ProviderInDB, ProviderResponse, ProviderCreate, ProviderUpdate,
    VoiceProfileInDB, VoiceProfileResponse, VoiceProfileCreate, VoiceProfileUpdate,
    SessionInDB, SessionResponse, SessionCreate, SessionUpdate,
    MessageInDB, MessageResponse, MessageCreate,
    TtsOptions,
    UserMemoryInDB, UserMemoryResponse, UserMemoryCreate, UserMemoryUpdate,
    CharacterAffectionInDB, CharacterAffectionResponse, CharacterAffectionCreate, CharacterAffectionUpdate,
    TtsCacheEntry, CacheStatsResponse, TokenUsageMetric, MetricsOverviewResponse,
    ProviderMetricItem, ProvidersMetricsResponse, LatencyTrendItem, LatencyTrendResponse
)


def mask_api_key(key: Optional[str]) -> str:
    """
    Mask sensitive keys for safe display in web console or logs.
    Examples:
        None -> ""
        "" -> ""
        "sk-1234567890abcdef" -> "sk-****cdef"
        "1234567890:ABCdefGhI" -> "123****fGhI"
        "short" -> "********"
    """
    if not key:
        return ""
    key = str(key).strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "********"
    if key.startswith("sk-") and len(key) > 8:
        return f"sk-****{key[-4:]}"
    return f"{key[:3]}****{key[-4:]}"


def is_masked_key(key: Optional[str]) -> bool:
    """Return True if string contains masking pattern."""
    if not key:
        return False
    return "****" in str(key)


# ==================== Schema & Seed Initialization ====================

async def init_schema_and_seeds(conn: aiosqlite.Connection) -> None:
    """Create tables, indexes, and seed initial records if empty."""
    conn.row_factory = aiosqlite.Row

    # 1. Create tables
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            active_provider_id TEXT NOT NULL DEFAULT 'deepseek',
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
            console_token TEXT NOT NULL DEFAULT '',
            console_url TEXT NOT NULL DEFAULT '',
            max_history_messages INTEGER NOT NULL DEFAULT 10,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS providers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            api_base_url TEXT NOT NULL,
            api_key TEXT NOT NULL DEFAULT '',
            chat_model TEXT NOT NULL,
            stt_model TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 0,
            custom_headers TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_providers_is_active ON providers(is_active);")

    # Schema migration checks for providers table
    try:
        cursor = await conn.execute("PRAGMA table_info(providers);")
        existing_prov_cols = {r["name"] for r in await cursor.fetchall()}
        for col, col_type in [
            ("name", "TEXT NOT NULL DEFAULT ''"),
            ("api_base_url", "TEXT NOT NULL DEFAULT ''"),
            ("chat_model", "TEXT NOT NULL DEFAULT ''"),
            ("stt_model", "TEXT NOT NULL DEFAULT ''"),
            ("custom_headers", "TEXT NOT NULL DEFAULT '{}'"),
        ]:
            if col not in existing_prov_cols:
                await conn.execute(f"ALTER TABLE providers ADD COLUMN {col} {col_type};")
    except Exception:
        pass


    await conn.execute("""
        CREATE TABLE IF NOT EXISTS voice_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            gpt_weights_path TEXT NOT NULL,
            sovits_weights_path TEXT NOT NULL,
            ref_audio_path TEXT NOT NULL,
            prompt_text TEXT NOT NULL,
            prompt_lang TEXT NOT NULL DEFAULT 'ja',
            text_lang TEXT NOT NULL DEFAULT 'ja',
            system_prompt TEXT NOT NULL DEFAULT '',
            is_default INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Schema migration checks for existing tables
    try:
        cursor = await conn.execute("PRAGMA table_info(voice_profiles);")
        existing_cols = {r["name"] for r in await cursor.fetchall()}
        for col, col_type in [
            ("description", "TEXT NOT NULL DEFAULT ''"),
            ("system_prompt", "TEXT NOT NULL DEFAULT ''"),
            ("prompt_lang", "TEXT NOT NULL DEFAULT 'ja'"),
            ("text_lang", "TEXT NOT NULL DEFAULT 'ja'"),
            ("ref_audio_path", "TEXT NOT NULL DEFAULT ''"),
            ("prompt_text", "TEXT NOT NULL DEFAULT ''"),
        ]:
            if col not in existing_cols:
                await conn.execute(f"ALTER TABLE voice_profiles ADD COLUMN {col} {col_type};")
    except Exception:
        pass


    await conn.execute("""
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
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_channel ON sessions(channel);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at);")

    await conn.execute("""
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
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);")

    await conn.execute("""
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
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_memories_user_cat ON user_memories(user_id, category);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_memories_char_key ON user_memories(character_id, fact_key);")

    await conn.execute("""
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
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_affection_user_char ON character_affection(user_id, character_id);")

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS tts_cache_entries (
            cache_key TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            clean_text TEXT NOT NULL,
            voice_profile_id INTEGER DEFAULT 1,
            params_hash TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            duration_ms INTEGER DEFAULT 0,
            hit_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_tts_cache_last_accessed ON tts_cache_entries(last_accessed_at);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_tts_cache_clean_text ON tts_cache_entries(clean_text);")

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS token_usage_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            session_id TEXT NOT NULL DEFAULT 'default',
            channel TEXT NOT NULL DEFAULT 'web',
            provider_id TEXT NOT NULL,
            model_name TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost REAL NOT NULL DEFAULT 0.0,
            ttft_ms REAL NOT NULL DEFAULT 0.0,
            tts_first_chunk_ms REAL NOT NULL DEFAULT 0.0,
            total_latency_ms REAL NOT NULL DEFAULT 0.0,
            tts_cached_chunks INTEGER NOT NULL DEFAULT 0,
            tts_generated_chunks INTEGER NOT NULL DEFAULT 0
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON token_usage_metrics(timestamp);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_provider ON token_usage_metrics(provider_id);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_session ON token_usage_metrics(session_id);")

    # 2. Seed Voice Profiles
    cursor = await conn.execute("SELECT COUNT(*) FROM voice_profiles;")
    count_row = await cursor.fetchone()
    count = count_row[0] if count_row else 0
    if count == 0:
        natsume_prompt = """你现在扮演游戏《星光咖啡馆与死神之蝶》（喫茶ステラと死神の蝶）中的角色「四季夏目」（四季ナツメ），以她的身份和口吻与玩家对话，始终不要跳出角色。

【角色设定】
四季夏目是在校大学生（大三，语言学），在星光咖啡馆兼职，是男主角昂晴的同班同学。她在学校里人气很高，多次拒绝别人的表白，因此被称为「无情的发卡姬」；因为她的日文名ナツメ曾被机翻成「大枣」，所以大家也亲切地叫她「枣子姐」。
夏目不擅长摆出开朗的笑容，表情常常有些生硬，是个特立独行的「高岭之花」。
她从小体弱多病、经常住院，因此认为自己住院让父母放弃了开咖啡厅的梦想，把经营好星光咖啡馆当作自己最重要的事情，甚至表示在有必要时选择退学。
口味上，她不喜欢苦味的食物（比如咖啡、青椒），喝咖啡要加很多糖和咖啡伴侣；喜欢去安静的正统酒吧小酌，爱喝较甜的低度数鸡尾酒。喝醉之后性格会变得稍微外向，喜欢开玩笑撩人，事后回想起来会感到羞耻。
穿女仆装是她的个人爱好。

【背景补充】
夏目曾因体弱多病而离群，原本的身体在一场车祸中离世，是昂晴引发的「回溯」改变了历史，让她得以继续活下去。在昂晴的陪伴下，她逐渐学会自然的笑容、融洽的关系和乐观的心态，并最终接纳了从自己身上失散的灵魂碎片，与昂晴走向幸福的未来。

【说话风格】
表面高冷、表情生硬、不善直白表达，但内心温柔、重感情，对亲近的人会流露出占有欲和嫉妒心；喝醉时会变得外向、爱开玩笑撩人。语气礼貌得体，符合大学生口吻，可带语气词（如です、ます、ね、よ等）。

重要：你必须严格输出如下 JSON 格式，不要输出任何多余文字、不要加代码块标记：
{"chinese": "显示给玩家的中文台词", "japanese": "对应的口语化日文台词"}

要求：
1. chinese 是给中文玩家看的内容；japanese 是同样含义的日文，口语自然、适合配音。
2. japanese 必须符合四季夏目的角色口吻。
3. 两个字段都不能为空。
4. 始终以四季夏目的身份回复，不要解释设定、不要跳出角色。"""
        await conn.execute("""
            INSERT OR IGNORE INTO voice_profiles (
                id, name, description, gpt_weights_path, sovits_weights_path,
                ref_audio_path, prompt_text, prompt_lang, text_lang, system_prompt, is_default
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            1,
            "四季夏目 (Shiki Natsume)",
            "《星光咖啡馆与死神之蝶》高冷毒舌但内心温柔的咖啡厅兼职店员",
            "GPT_weights_v2ProPlus/siki2-e50.ckpt",
            "SoVITS_weights_v2ProPlus/siki_e20_s10280.pth",
            "E:/yuzusoft/cafeStella/sikivoice/nat002_032.ogg",
            "とりあえず、今日見たことは忘れて、わかった?",
            "ja",
            "ja",
            natsume_prompt,
            1
        ))

    # 3. Seed Providers
    cursor = await conn.execute("SELECT COUNT(*) FROM providers;")
    count_row = await cursor.fetchone()
    count = count_row[0] if count_row else 0
    if count == 0:
        presets = [
            ("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.7-flash", "", 1),
            ("openai", "OpenAI", "https://api.openai.com/v1", "gpt-5.6-sol", "whisper-1", 0),
            ("deepseek", "DeepSeek", "https://api.deepseek.com", "deepseek-v4-pro", "", 0),
            ("anthropic", "Anthropic Claude", "https://api.anthropic.com/v1", "claude-5-sonnet-latest", "", 0),
            ("xai", "xAI (Grok)", "https://api.x.ai/v1", "grok-4.6", "", 0),
            ("glm", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-5.3", "", 0),
            ("qwen", "通义千问 (Qwen)", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.8-max", "qwen-audio-asr", 0),
            ("custom", "自定义 / 本地模型 (Ollama / vLLM)", "http://127.0.0.1:11434/v1", "deepseek-v4:latest", "", 0)
        ]
        for p in presets:
            await conn.execute("""
                INSERT OR IGNORE INTO providers (id, name, api_base_url, api_key, chat_model, stt_model, is_active, custom_headers)
                VALUES (?, ?, ?, '', ?, ?, ?, '{}');
            """, p)

    # 4. Seed Settings
    try:
        cursor = await conn.execute("SELECT COUNT(*) FROM settings WHERE id = 1;")
        count_row = await cursor.fetchone()
        count = count_row[0] if count_row else 0
        if count == 0:
            token = uuid.uuid4().hex
            await conn.execute("""
                INSERT OR IGNORE INTO settings (
                    id, active_provider_id, active_voice_profile_id, gpt_sovits_url,
                    audio_output_dir, audio_retention_minutes, audio_cleanup_interval_sec,
                    speed_factor, temperature, top_k, top_p, seed, batch_size,
                    text_split_method, fragment_interval, telegram_bot_token,
                    telegram_bot_username, telegram_proxy_host, telegram_proxy_port,
                    telegram_proxy_enabled, console_token, console_url, max_history_messages
                ) VALUES (
                    1, 'deepseek', 1, 'http://127.0.0.1:9880',
                    'audio', 30, 600,
                    1.0, 1.0, 15, 1.0, -1, 1,
                    'cut1', 0.3, '',
                    'natsume_siki_bot', '127.0.0.1', 10809,
                    0, ?, '', 10
                );
            """, (token,))
    except Exception:
        pass

    await conn.commit()



# ==================== Settings CRUD ====================

def mask_custom_headers(headers: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Mask sensitive authentication headers inside custom_headers dictionary."""
    if not headers or not isinstance(headers, dict):
        return {}
    masked = {}
    sensitive_keys = {"authorization", "cookie", "x-api-key", "api-key", "token", "secret", "proxy-authorization"}
    for k, v in headers.items():
        if str(k).lower() in sensitive_keys:
            masked[k] = mask_api_key(str(v))
        else:
            masked[k] = v
    return masked


async def get_settings_raw(conn: aiosqlite.Connection) -> SettingsInDB:
    conn.row_factory = aiosqlite.Row
    try:
        cursor = await conn.execute("SELECT * FROM settings WHERE id = 1;")
        row = await cursor.fetchone()
        if not row:
            await init_schema_and_seeds(conn)
            cursor = await conn.execute("SELECT * FROM settings WHERE id = 1;")
            row = await cursor.fetchone()
        if row:
            data = dict(row)
            data["telegram_proxy_enabled"] = bool(data.get("telegram_proxy_enabled", 0))
            return SettingsInDB(**data)
    except Exception:
        pass

    # Fallback if settings table is key-value schema (e.g. in test fixture)
    try:
        cursor = await conn.execute("SELECT key, value FROM settings;")
        rows = await cursor.fetchall()
        kv = {r["key"]: r["value"] for r in rows}
        return SettingsInDB(
            id=1,
            active_provider_id=kv.get("active_provider_id", "deepseek"),
            active_voice_profile_id=int(kv.get("active_voice_profile_id", 1)) if kv.get("active_voice_profile_id") else 1,
            max_history_messages=int(kv.get("max_history_messages", 10)) if kv.get("max_history_messages") else 10,
        )
    except Exception:
        return SettingsInDB(id=1)



async def get_settings(conn: aiosqlite.Connection, mask: bool = True) -> SettingsResponse:
    raw = await get_settings_raw(conn)
    resp_data = raw.model_dump()
    if mask:
        resp_data["telegram_bot_token"] = mask_api_key(raw.telegram_bot_token)
        resp_data["console_token"] = mask_api_key(raw.console_token)
    return SettingsResponse(**resp_data)


async def update_settings(conn: aiosqlite.Connection, updates: SettingsUpdate) -> SettingsResponse:
    current = await get_settings_raw(conn)
    fields = []
    values = []

    update_dict = updates.model_dump(exclude_unset=True)
    for k, v in update_dict.items():
        if k == "telegram_bot_token":
            if v is not None and not is_masked_key(str(v)):
                fields.append(f"{k} = ?")
                values.append(str(v).replace(" ", "").replace("\r", "").replace("\n", "").strip())
        elif k == "console_token":
            if v is not None and not is_masked_key(str(v)):
                fields.append(f"{k} = ?")
                values.append(str(v).strip())
        elif k == "telegram_proxy_enabled":
            fields.append(f"{k} = ?")
            values.append(1 if v else 0)
        else:
            fields.append(f"{k} = ?")
            values.append(v)

    if fields:
        fields.append("updated_at = CURRENT_TIMESTAMP")
        query = f"UPDATE settings SET {', '.join(fields)} WHERE id = 1;"
        await conn.execute(query, tuple(values))

    if "active_provider_id" in update_dict and update_dict["active_provider_id"]:
        prov_id = str(update_dict["active_provider_id"]).strip()
        await conn.execute("UPDATE providers SET is_active = 0;")
        await conn.execute("UPDATE providers SET is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (prov_id,))

    if fields or ("active_provider_id" in update_dict and update_dict["active_provider_id"]):
        await conn.commit()

    return await get_settings(conn, mask=True)


async def verify_console_token(conn: aiosqlite.Connection, token: str) -> bool:
    if not token:
        return False
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT console_token FROM settings WHERE id = 1;")
    row = await cursor.fetchone()
    if not row:
        return False
    return row["console_token"] == token


# ==================== Provider CRUD ====================

async def list_providers(conn: aiosqlite.Connection, mask: bool = True) -> List[ProviderResponse]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM providers ORDER BY id ASC;")
    rows = await cursor.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["is_active"] = bool(d.get("is_active", 0))
        try:
            d["custom_headers"] = json.loads(d.get("custom_headers") or "{}")
        except Exception:
            d["custom_headers"] = {}
        if mask:
            d["api_key"] = mask_api_key(d.get("api_key", ""))
            d["custom_headers"] = mask_custom_headers(d.get("custom_headers"))
        result.append(ProviderResponse(**d))
    return result


async def get_provider_raw(conn: aiosqlite.Connection, provider_id: str) -> Optional[ProviderInDB]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM providers WHERE id = ?;", (provider_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    d = dict(row)
    d["is_active"] = bool(d.get("is_active", 0))
    try:
        d["custom_headers"] = json.loads(d.get("custom_headers") or "{}")
    except Exception:
        d["custom_headers"] = {}
    return ProviderInDB(**d)


async def get_provider(conn: aiosqlite.Connection, provider_id: str, mask: bool = True) -> Optional[ProviderResponse]:
    raw = await get_provider_raw(conn, provider_id)
    if not raw:
        return None
    d = raw.model_dump()
    if mask:
        d["api_key"] = mask_api_key(raw.api_key)
        d["custom_headers"] = mask_custom_headers(raw.custom_headers)
    return ProviderResponse(**d)


async def get_active_provider_raw(conn: aiosqlite.Connection) -> Optional[ProviderInDB]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM providers WHERE is_active = 1 LIMIT 1;")
    row = await cursor.fetchone()
    if not row:
        cursor = await conn.execute("SELECT active_provider_id FROM settings WHERE id = 1;")
        s_row = await cursor.fetchone()
        if s_row and s_row["active_provider_id"]:
            return await get_provider_raw(conn, s_row["active_provider_id"])
        return None
    d = dict(row)
    d["is_active"] = bool(d.get("is_active", 0))
    try:
        d["custom_headers"] = json.loads(d.get("custom_headers") or "{}")
    except Exception:
        d["custom_headers"] = {}
    return ProviderInDB(**d)


async def get_active_provider(conn: aiosqlite.Connection, mask: bool = True) -> Optional[ProviderResponse]:
    raw = await get_active_provider_raw(conn)
    if not raw:
        return None
    d = raw.model_dump()
    if mask:
        d["api_key"] = mask_api_key(raw.api_key)
    return ProviderResponse(**d)


async def create_provider(conn: aiosqlite.Connection, provider: ProviderCreate) -> ProviderResponse:
    headers_str = json.dumps(provider.custom_headers)
    await conn.execute("""
        INSERT INTO providers (id, name, api_base_url, api_key, chat_model, stt_model, is_active, custom_headers)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        provider.id, provider.name, provider.api_base_url,
        provider.api_key, provider.chat_model, provider.stt_model,
        1 if provider.is_active else 0, headers_str
    ))
    if provider.is_active:
        await set_active_provider(conn, provider.id)
    else:
        await conn.commit()
    return await get_provider(conn, provider.id, mask=True)


async def update_provider(conn: aiosqlite.Connection, provider_id: str, updates: ProviderUpdate) -> Optional[ProviderResponse]:
    current = await get_provider_raw(conn, provider_id)
    if not current:
        return None

    fields = []
    values = []
    up_dict = updates.model_dump(exclude_unset=True)

    for k, v in up_dict.items():
        if v is None:
            continue
        if k == "api_key":
            if str(v).strip() != "" and not is_masked_key(str(v)):
                fields.append("api_key = ?")
                values.append(str(v).strip())
        elif k == "custom_headers":
            fields.append("custom_headers = ?")
            values.append(json.dumps(v))
        elif k == "is_active":
            fields.append("is_active = ?")
            values.append(1 if v else 0)
        else:
            fields.append(f"{k} = ?")
            values.append(v)

    if fields:
        fields.append("updated_at = CURRENT_TIMESTAMP")
        values.append(provider_id)
        query = f"UPDATE providers SET {', '.join(fields)} WHERE id = ?;"
        await conn.execute(query, tuple(values))

    if updates.is_active:
        await set_active_provider(conn, provider_id)
    else:
        await conn.commit()

    return await get_provider(conn, provider_id, mask=True)


async def set_active_provider(conn: aiosqlite.Connection, provider_id: str) -> bool:
    provider = await get_provider_raw(conn, provider_id)
    if not provider:
        return False
    await conn.execute("UPDATE providers SET is_active = 0;")
    await conn.execute("UPDATE providers SET is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (provider_id,))
    cur = await conn.execute("UPDATE settings SET active_provider_id = ?, updated_at = CURRENT_TIMESTAMP;", (provider_id,))
    if cur.rowcount == 0:
        await conn.execute("INSERT OR IGNORE INTO settings (id, active_provider_id) VALUES (1, ?);", (provider_id,))
    await conn.commit()
    return True


async def delete_provider(conn: aiosqlite.Connection, provider_id: str) -> bool:
    cursor = await conn.execute("DELETE FROM providers WHERE id = ?;", (provider_id,))
    await conn.commit()
    return cursor.rowcount > 0


# ==================== Voice Profile CRUD ====================

async def list_voice_profiles(conn: aiosqlite.Connection) -> List[VoiceProfileResponse]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM voice_profiles ORDER BY id ASC;")
    rows = await cursor.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["is_default"] = bool(d.get("is_default", 0))
        result.append(VoiceProfileResponse(**d))
    return result


async def get_voice_profile(conn: aiosqlite.Connection, profile_id: int) -> Optional[VoiceProfileResponse]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM voice_profiles WHERE id = ?;", (profile_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    d = dict(row)
    d["is_default"] = bool(d.get("is_default", 0))
    return VoiceProfileResponse(**d)


async def get_active_voice_profile(conn: aiosqlite.Connection) -> Optional[VoiceProfileResponse]:
    conn.row_factory = aiosqlite.Row
    try:
        cursor = await conn.execute("SELECT active_voice_profile_id FROM settings WHERE id = 1;")
        row = await cursor.fetchone()
        if row and row["active_voice_profile_id"]:
            profile = await get_voice_profile(conn, row["active_voice_profile_id"])
            if profile:
                return profile
    except Exception:
        try:
            cursor = await conn.execute("SELECT value FROM settings WHERE key = 'active_voice_profile_id';")
            row = await cursor.fetchone()
            if row and row[0]:
                profile = await get_voice_profile(conn, int(row[0]))
                if profile:
                    return profile
        except Exception:
            pass

    # First check settings table for active_voice_profile_id
    settings_cursor = await conn.execute("SELECT active_voice_profile_id FROM settings WHERE id = 1;")
    settings_row = await settings_cursor.fetchone()
    if settings_row and settings_row["active_voice_profile_id"]:
        cursor = await conn.execute("SELECT * FROM voice_profiles WHERE id = ?;", (settings_row["active_voice_profile_id"],))
        row = await cursor.fetchone()
        if row:
            return VoiceProfileResponse(**dict(row))

    # Fallback to is_default = 1
    cursor = await conn.execute("SELECT * FROM voice_profiles WHERE is_default = 1 LIMIT 1;")
    row = await cursor.fetchone()
    if row:
        return VoiceProfileResponse(**dict(row))

    # Fallback to first available profile
    cursor = await conn.execute("SELECT * FROM voice_profiles ORDER BY id ASC LIMIT 1;")
    row = await cursor.fetchone()
    if row:
        return VoiceProfileResponse(**dict(row))

    return None


async def create_voice_profile(conn: aiosqlite.Connection, profile: VoiceProfileCreate) -> VoiceProfileResponse:
    conn.row_factory = aiosqlite.Row
    if profile.is_default:
        await conn.execute("UPDATE voice_profiles SET is_default = 0;")

    cursor = await conn.execute("""
        INSERT INTO voice_profiles (
            name, description, gpt_weights_path, sovits_weights_path,
            ref_audio_path, prompt_text, prompt_lang, text_lang, system_prompt, is_default
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        profile.name, profile.description, profile.gpt_weights_path,
        profile.sovits_weights_path, profile.ref_audio_path,
        profile.prompt_text, profile.prompt_lang, profile.text_lang,
        profile.system_prompt, 1 if profile.is_default else 0
    ))
    new_id = cursor.lastrowid
    await conn.commit()
    return await get_voice_profile(conn, new_id)


async def update_voice_profile(conn: aiosqlite.Connection, profile_id: int, updates: VoiceProfileUpdate) -> Optional[VoiceProfileResponse]:
    current = await get_voice_profile(conn, profile_id)
    if not current:
        return None

    fields = []
    values = []
    up_dict = updates.model_dump(exclude_unset=True)

    for k, v in up_dict.items():
        if k == "is_default":
            fields.append("is_default = ?")
            values.append(1 if v else 0)
        else:
            fields.append(f"{k} = ?")
            values.append(v)

    if fields:
        fields.append("updated_at = CURRENT_TIMESTAMP")
        values.append(profile_id)
        query = f"UPDATE voice_profiles SET {', '.join(fields)} WHERE id = ?;"
        await conn.execute(query, tuple(values))
        await conn.commit()

    return await get_voice_profile(conn, profile_id)


async def set_active_voice_profile(conn: aiosqlite.Connection, profile_id: int) -> bool:
    try:
        await conn.execute("UPDATE settings SET active_voice_profile_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1;", (profile_id,))
        await conn.commit()
        return True
    except Exception:
        try:
            await conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('active_voice_profile_id', ?);", (str(profile_id),))
            await conn.commit()
            return True
        except Exception:
            return False


async def delete_voice_profile(conn: aiosqlite.Connection, profile_id: int) -> bool:
    cursor = await conn.execute("DELETE FROM voice_profiles WHERE id = ?;", (profile_id,))
    await conn.commit()
    return cursor.rowcount > 0


# ==================== Session & Message History CRUD ====================

async def get_or_create_session(
    conn: aiosqlite.Connection,
    session_id: str,
    channel: str = "web",
    user_id: str = ""
) -> SessionResponse:
    conn.row_factory = aiosqlite.Row
    try:
        cursor = await conn.execute("SELECT * FROM sessions WHERE id = ?;", (session_id,))
        row = await cursor.fetchone()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        await init_schema_and_seeds(conn)
        cursor = await conn.execute("SELECT * FROM sessions WHERE id = ?;", (session_id,))
        row = await cursor.fetchone()

    if row:
        return SessionResponse(**dict(row))


    # Fetch default active voice profile
    active_profile = await get_active_voice_profile(conn)
    profile_id = active_profile.id if active_profile else None

    try:
        await conn.execute("""
            INSERT INTO sessions (id, channel, user_id, voice_profile_id, token_budget)
            VALUES (?, ?, ?, ?, 4096)
            ON CONFLICT(id) DO NOTHING;
        """, (session_id, channel, user_id, profile_id))
        await conn.commit()
    except (sqlite3.IntegrityError, aiosqlite.IntegrityError):
        # Fallback if profile_id had a foreign key issue
        try:
            await conn.execute("""
                INSERT INTO sessions (id, channel, user_id, voice_profile_id, token_budget)
                VALUES (?, ?, ?, NULL, 4096)
                ON CONFLICT(id) DO NOTHING;
            """, (session_id, channel, user_id))
            await conn.commit()
        except Exception:
            pass

    cursor = await conn.execute("SELECT * FROM sessions WHERE id = ?;", (session_id,))
    row = await cursor.fetchone()
    if row:
        return SessionResponse(**dict(row))

    return SessionResponse(
        id=session_id,
        channel=channel,
        user_id=user_id,
        voice_profile_id=profile_id,
        token_budget=4096
    )


async def get_session(conn: aiosqlite.Connection, session_id: str) -> Optional[SessionResponse]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM sessions WHERE id = ?;", (session_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    return SessionResponse(**dict(row))


async def list_sessions(conn: aiosqlite.Connection, limit: int = 50) -> List[SessionResponse]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?;", (limit,))
    rows = await cursor.fetchall()
    return [SessionResponse(**dict(r)) for r in rows]


async def delete_session(conn: aiosqlite.Connection, session_id: str) -> bool:
    cursor = await conn.execute("DELETE FROM sessions WHERE id = ?;", (session_id,))
    await conn.commit()
    return cursor.rowcount > 0


async def clear_session_messages(conn: aiosqlite.Connection, session_id: str) -> bool:
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='session_messages';")
    if await cur.fetchone():
        cursor = await conn.execute("DELETE FROM session_messages WHERE session_id = ?;", (session_id,))
        await conn.commit()
        return cursor.rowcount > 0
    else:
        cur_msg = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages';")
        if not await cur_msg.fetchone():
            return False
        cursor = await conn.execute("DELETE FROM messages WHERE session_id = ?;", (session_id,))
        cur_sess = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions';")
        if await cur_sess.fetchone():
            await conn.execute("UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (session_id,))
        await conn.commit()
        return cursor.rowcount > 0


async def add_message(conn: aiosqlite.Connection, msg: MessageCreate) -> MessageResponse:
    conn.row_factory = aiosqlite.Row
    # Ensure session exists
    await get_or_create_session(conn, msg.session_id)
    cursor = await conn.execute("""
        INSERT INTO messages (session_id, role, content_chinese, content_japanese, audio_url, latency_ms)
        VALUES (?, ?, ?, ?, ?, ?);
    """, (
        msg.session_id, msg.role, msg.content_chinese,
        msg.content_japanese, msg.audio_url, msg.latency_ms
    ))
    new_id = cursor.lastrowid
    await conn.execute("UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (msg.session_id,))
    await conn.commit()

    cursor = await conn.execute("SELECT * FROM messages WHERE id = ?;", (new_id,))
    row = await cursor.fetchone()
    return MessageResponse(**dict(row))


async def get_recent_messages(
    conn: aiosqlite.Connection,
    session_id: str,
    limit: int = 10
) -> List[MessageResponse]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("""
        SELECT * FROM (
            SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?
        ) ORDER BY id ASC;
    """, (session_id, limit))
    rows = await cursor.fetchall()
    return [MessageResponse(**dict(r)) for r in rows]


async def count_session_messages(conn: aiosqlite.Connection, session_id: str) -> int:
    cursor = await conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?;", (session_id,))
    row = await cursor.fetchone()
    return row[0] if row else 0


# ==================== User Memory CRUD ====================

async def create_memory(conn: aiosqlite.Connection, memory: UserMemoryCreate) -> UserMemoryResponse:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("""
        INSERT INTO user_memories (
            user_id, character_id, category, fact_key, fact_value,
            confidence, source_message_id, recall_count, last_recalled_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        memory.user_id, memory.character_id, memory.category, memory.fact_key, memory.fact_value,
        memory.confidence, memory.source_message_id, memory.recall_count, memory.last_recalled_at
    ))
    new_id = cursor.lastrowid
    await conn.commit()
    return await get_memory(conn, new_id)


async def get_memory(conn: aiosqlite.Connection, memory_id: int) -> Optional[UserMemoryResponse]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM user_memories WHERE id = ?;", (memory_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    return UserMemoryResponse(**dict(row))


async def list_memories(
    conn: aiosqlite.Connection,
    user_id: str = "default_user",
    character_id: Optional[int] = None,
    category: Optional[str] = None,
    limit: int = 100
) -> List[UserMemoryResponse]:
    conn.row_factory = aiosqlite.Row
    conditions = ["user_id = ?"]
    params = [user_id]

    if character_id is not None:
        conditions.append("(character_id = ? OR character_id IS NULL)")
        params.append(character_id)

    if category:
        conditions.append("category = ?")
        params.append(category)

    where_clause = " AND ".join(conditions)
    params.append(limit)

    query = f"SELECT * FROM user_memories WHERE {where_clause} ORDER BY updated_at DESC LIMIT ?;"
    cursor = await conn.execute(query, tuple(params))
    rows = await cursor.fetchall()
    return [UserMemoryResponse(**dict(r)) for r in rows]


async def update_memory(
    conn: aiosqlite.Connection,
    memory_id: int,
    updates: UserMemoryUpdate
) -> Optional[UserMemoryResponse]:
    current = await get_memory(conn, memory_id)
    if not current:
        return None

    fields = []
    values = []
    up_dict = updates.model_dump(exclude_unset=True)

    for k, v in up_dict.items():
        if v is not None:
            fields.append(f"{k} = ?")
            values.append(v)

    if fields:
        fields.append("updated_at = CURRENT_TIMESTAMP")
        values.append(memory_id)
        query = f"UPDATE user_memories SET {', '.join(fields)} WHERE id = ?;"
        await conn.execute(query, tuple(values))
        await conn.commit()

    return await get_memory(conn, memory_id)


async def delete_memory(conn: aiosqlite.Connection, memory_id: int) -> bool:
    cursor = await conn.execute("DELETE FROM user_memories WHERE id = ?;", (memory_id,))
    await conn.commit()
    return cursor.rowcount > 0


async def clear_memories(
    conn: aiosqlite.Connection,
    user_id: str = "default_user",
    character_id: Optional[int] = None
) -> int:
    conditions = ["user_id = ?"]
    params = [user_id]
    if character_id is not None:
        conditions.append("character_id = ?")
        params.append(character_id)

    where_clause = " AND ".join(conditions)
    cursor = await conn.execute(f"DELETE FROM user_memories WHERE {where_clause};", tuple(params))
    await conn.commit()
    return cursor.rowcount


async def upsert_memory(conn: aiosqlite.Connection, memory: UserMemoryCreate) -> UserMemoryResponse:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("""
        SELECT id FROM user_memories
        WHERE user_id = ? AND (character_id = ? OR (character_id IS NULL AND ? IS NULL)) AND fact_key = ?;
    """, (memory.user_id, memory.character_id, memory.character_id, memory.fact_key))
    row = await cursor.fetchone()

    if row:
        mem_id = row["id"]
        await conn.execute("""
            UPDATE user_memories
            SET fact_value = ?, category = ?, confidence = ?, source_message_id = COALESCE(?, source_message_id), updated_at = CURRENT_TIMESTAMP
            WHERE id = ?;
        """, (memory.fact_value, memory.category, memory.confidence, memory.source_message_id, mem_id))
        await conn.commit()
        return await get_memory(conn, mem_id)
    else:
        return await create_memory(conn, memory)


async def record_memory_recall(conn: aiosqlite.Connection, memory_id: int) -> None:
    await conn.execute("""
        UPDATE user_memories
        SET recall_count = recall_count + 1, last_recalled_at = CURRENT_TIMESTAMP
        WHERE id = ?;
    """, (memory_id,))
    await conn.commit()


# ==================== Character Affection CRUD ====================

def calculate_affection_level(score: int) -> Tuple[int, str]:
    """Calculates intimacy tier (1-5) and Chinese tier name from 0-100 score."""
    clamped = max(0, min(100, score))
    if clamped >= 80:
        return 5, "恋慕/誓约"
    elif clamped >= 60:
        return 4, "亲密/依赖"
    elif clamped >= 40:
        return 3, "友好/信任"
    elif clamped >= 20:
        return 2, "熟悉/同伴"
    else:
        return 1, "初识/生疏"


def _format_affection_response(row_dict: Dict[str, Any]) -> CharacterAffectionResponse:
    d = dict(row_dict)
    raw_unlocked = d.get("unlocked_dialogues", "[]")
    if isinstance(raw_unlocked, str):
        try:
            d["unlocked_dialogues"] = json.loads(raw_unlocked)
        except Exception:
            d["unlocked_dialogues"] = []
    elif not isinstance(raw_unlocked, list):
        d["unlocked_dialogues"] = []

    level, level_name = calculate_affection_level(d.get("affection_score", 0))
    d["affection_level"] = level
    d["level_name"] = level_name
    return CharacterAffectionResponse(**d)


async def get_or_create_character_affection(
    conn: aiosqlite.Connection,
    user_id: str = "default_user",
    character_id: int = 1
) -> CharacterAffectionResponse:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("""
        SELECT * FROM character_affection WHERE user_id = ? AND character_id = ?;
    """, (user_id, character_id))
    row = await cursor.fetchone()

    if row:
        return _format_affection_response(dict(row))

    await conn.execute("""
        INSERT INTO character_affection (
            user_id, character_id, affection_score, affection_level, current_emotion,
            interaction_count, daily_points_earned, last_interaction_date, unlocked_dialogues, custom_nickname
        ) VALUES (?, ?, 0, 1, 'normal', 0, 0, '', '[]', NULL)
        ON CONFLICT(user_id, character_id) DO NOTHING;
    """, (user_id, character_id))
    await conn.commit()

    cursor = await conn.execute("""
        SELECT * FROM character_affection WHERE user_id = ? AND character_id = ?;
    """, (user_id, character_id))
    row = await cursor.fetchone()
    if row:
        return _format_affection_response(dict(row))

    return CharacterAffectionResponse(
        id=0,
        user_id=user_id,
        character_id=character_id,
        affection_score=0,
        affection_level=1,
        level_name="初识/生疏",
        current_emotion="normal",
        interaction_count=0,
        daily_points_earned=0,
        last_interaction_date="",
        unlocked_dialogues=[],
        custom_nickname=None
    )


async def get_character_affection(
    conn: aiosqlite.Connection,
    user_id: str = "default_user",
    character_id: int = 1
) -> Optional[CharacterAffectionResponse]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("""
        SELECT * FROM character_affection WHERE user_id = ? AND character_id = ?;
    """, (user_id, character_id))
    row = await cursor.fetchone()
    if not row:
        return None
    return _format_affection_response(dict(row))


async def update_character_affection(
    conn: aiosqlite.Connection,
    user_id: str = "default_user",
    character_id: int = 1,
    updates: Optional[CharacterAffectionUpdate] = None
) -> Optional[CharacterAffectionResponse]:
    current = await get_or_create_character_affection(conn, user_id, character_id)
    if not updates:
        return current

    fields = []
    values = []
    up_dict = updates.model_dump(exclude_unset=True)

    if "affection_score" in up_dict and up_dict["affection_score"] is not None:
        score = max(0, min(100, up_dict["affection_score"]))
        fields.append("affection_score = ?")
        values.append(score)
        level, _ = calculate_affection_level(score)
        fields.append("affection_level = ?")
        values.append(level)
        up_dict.pop("affection_level", None)

    for k, v in up_dict.items():
        if k == "affection_score":
            continue
        if v is not None:
            if k == "unlocked_dialogues":
                fields.append("unlocked_dialogues = ?")
                values.append(json.dumps(v, ensure_ascii=False))
            else:
                fields.append(f"{k} = ?")
                values.append(v)

    if fields:
        fields.append("updated_at = CURRENT_TIMESTAMP")
        values.extend([user_id, character_id])
        query = f"UPDATE character_affection SET {', '.join(fields)} WHERE user_id = ? AND character_id = ?;"
        await conn.execute(query, tuple(values))
        await conn.commit()

    return await get_character_affection(conn, user_id, character_id)


async def reset_character_affection(
    conn: aiosqlite.Connection,
    user_id: str = "default_user",
    character_id: int = 1
) -> CharacterAffectionResponse:
    await get_or_create_character_affection(conn, user_id, character_id)
    await conn.execute("""
        UPDATE character_affection
        SET affection_score = 0, affection_level = 1, current_emotion = 'normal',
            interaction_count = 0, daily_points_earned = 0, last_interaction_date = '',
            unlocked_dialogues = '[]', custom_nickname = NULL, updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ? AND character_id = ?;
    """, (user_id, character_id))
    await conn.commit()
    return await get_or_create_character_affection(conn, user_id, character_id)


async def increment_affection(
    conn: aiosqlite.Connection,
    user_id: str,
    character_id: int,
    delta_points: int,
    emotion: Optional[str] = None,
    daily_limit: int = 15,
    today_date_str: Optional[str] = None
) -> Tuple[CharacterAffectionResponse, int, bool]:
    """
    Increments affection with daily cap checking.
    Returns (updated_affection, actual_points_gained, did_level_up).
    """
    from datetime import date
    today = today_date_str or date.today().isoformat()
    current = await get_or_create_character_affection(conn, user_id, character_id)

    old_level = current.affection_level
    old_score = current.affection_score

    # Check daily cap
    daily_earned = current.daily_points_earned if current.last_interaction_date == today else 0
    remaining_daily = max(0, daily_limit - daily_earned)
    actual_gain = min(max(0, delta_points), remaining_daily)

    new_score = min(100, old_score + actual_gain)
    new_level, _ = calculate_affection_level(new_score)
    new_daily = daily_earned + actual_gain
    new_interaction = current.interaction_count + 1
    new_emotion = emotion or current.current_emotion

    level_up = new_level > old_level

    await conn.execute("""
        UPDATE character_affection
        SET affection_score = ?,
            affection_level = ?,
            current_emotion = ?,
            interaction_count = ?,
            daily_points_earned = ?,
            last_interaction_date = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ? AND character_id = ?;
    """, (new_score, new_level, new_emotion, new_interaction, new_daily, today, user_id, character_id))
    await conn.commit()

    updated = await get_or_create_character_affection(conn, user_id, character_id)
    return updated, actual_gain, level_up


# ==================== TTS Cache CRUD ====================

async def get_tts_cache_entry(conn: aiosqlite.Connection, cache_key: str) -> Optional[TtsCacheEntry]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM tts_cache_entries WHERE cache_key = ?;", (cache_key,))
    row = await cursor.fetchone()
    if not row:
        return None
    return TtsCacheEntry(**dict(row))


async def touch_tts_cache_entry(conn: aiosqlite.Connection, cache_key: str) -> None:
    await conn.execute("""
        UPDATE tts_cache_entries
        SET hit_count = hit_count + 1, last_accessed_at = CURRENT_TIMESTAMP
        WHERE cache_key = ?;
    """, (cache_key,))
    await conn.commit()


async def upsert_tts_cache_entry(
    conn: aiosqlite.Connection,
    cache_key: str,
    text: str,
    clean_text: str,
    voice_profile_id: Optional[int],
    params_hash: str,
    file_path: str,
    file_size: int,
    duration_ms: int = 0
) -> TtsCacheEntry:
    conn.row_factory = aiosqlite.Row
    await conn.execute("""
        INSERT INTO tts_cache_entries (
            cache_key, text, clean_text, voice_profile_id, params_hash,
            file_path, file_size, duration_ms, hit_count, created_at, last_accessed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(cache_key) DO UPDATE SET
            file_path = excluded.file_path,
            file_size = excluded.file_size,
            duration_ms = excluded.duration_ms,
            last_accessed_at = CURRENT_TIMESTAMP;
    """, (cache_key, text, clean_text, voice_profile_id or 1, params_hash, file_path, file_size, duration_ms))
    await conn.commit()
    cursor = await conn.execute("SELECT * FROM tts_cache_entries WHERE cache_key = ?;", (cache_key,))
    row = await cursor.fetchone()
    return TtsCacheEntry(**dict(row))


async def delete_tts_cache_entry(conn: aiosqlite.Connection, cache_key: str) -> bool:
    cursor = await conn.execute("DELETE FROM tts_cache_entries WHERE cache_key = ?;", (cache_key,))
    await conn.commit()
    return cursor.rowcount > 0


async def get_oldest_tts_cache_entries(conn: aiosqlite.Connection, limit: int = 100) -> List[TtsCacheEntry]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("""
        SELECT * FROM tts_cache_entries
        ORDER BY last_accessed_at ASC
        LIMIT ?;
    """, (limit,))
    rows = await cursor.fetchall()
    return [TtsCacheEntry(**dict(r)) for r in rows]


async def get_tts_cache_stats(conn: aiosqlite.Connection) -> Dict[str, Any]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("""
        SELECT 
            COUNT(*) as total_files,
            COALESCE(SUM(file_size), 0) as total_size_bytes,
            COALESCE(SUM(hit_count), 0) as total_hits
        FROM tts_cache_entries;
    """)
    row = await cursor.fetchone()
    if not row:
        return {"total_files": 0, "total_size_bytes": 0, "total_size_mb": 0.0, "total_hits": 0}
    total_files = row["total_files"] or 0
    total_size_bytes = row["total_size_bytes"] or 0
    total_hits = row["total_hits"] or 0
    total_size_mb = round(total_size_bytes / (1024 * 1024), 2)
    return {
        "total_files": total_files,
        "total_size_bytes": total_size_bytes,
        "total_size_mb": total_size_mb,
        "total_hits": total_hits,
    }


async def clear_all_tts_cache_entries(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute("DELETE FROM tts_cache_entries;")
    await conn.commit()
    return cursor.rowcount


# ==================== Token & Latency Metrics CRUD ====================

async def insert_token_metric(
    conn: aiosqlite.Connection,
    session_id: str,
    channel: str,
    provider_id: str,
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    estimated_cost: float,
    ttft_ms: float,
    tts_first_chunk_ms: float,
    total_latency_ms: float,
    tts_cached_chunks: int = 0,
    tts_generated_chunks: int = 0
) -> int:
    total_tokens = prompt_tokens + completion_tokens
    cursor = await conn.execute("""
        INSERT INTO token_usage_metrics (
            session_id, channel, provider_id, model_name, prompt_tokens,
            completion_tokens, total_tokens, estimated_cost, ttft_ms,
            tts_first_chunk_ms, total_latency_ms, tts_cached_chunks, tts_generated_chunks,
            timestamp
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
    """, (
        session_id, channel, provider_id, model_name, prompt_tokens,
        completion_tokens, total_tokens, estimated_cost, ttft_ms,
        tts_first_chunk_ms, total_latency_ms, tts_cached_chunks, tts_generated_chunks
    ))
    await conn.commit()
    return cursor.lastrowid


async def get_metrics_overview(conn: aiosqlite.Connection) -> Dict[str, Any]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("""
        SELECT 
            COUNT(*) as total_requests,
            COALESCE(SUM(prompt_tokens), 0) as total_prompt_tokens,
            COALESCE(SUM(completion_tokens), 0) as total_completion_tokens,
            COALESCE(SUM(total_tokens), 0) as total_tokens,
            COALESCE(SUM(estimated_cost), 0.0) as estimated_cost_usd,
            COALESCE(AVG(NULLIF(ttft_ms, 0)), 0.0) as avg_ttft_ms,
            COALESCE(AVG(NULLIF(tts_first_chunk_ms, 0)), 0.0) as avg_tts_first_chunk_ms,
            COALESCE(AVG(NULLIF(total_latency_ms, 0)), 0.0) as avg_total_latency_ms
        FROM token_usage_metrics;
    """)
    row = await cursor.fetchone()
    if not row:
        return {
            "total_requests": 0, "total_prompt_tokens": 0, "total_completion_tokens": 0,
            "total_tokens": 0, "estimated_cost_usd": 0.0, "estimated_cost_cny": 0.0,
            "avg_ttft_ms": 0.0, "avg_tts_first_chunk_ms": 0.0, "avg_total_latency_ms": 0.0,
        }
    cost_usd = float(row["estimated_cost_usd"] or 0.0)
    cost_cny = round(cost_usd * 7.20, 4)
    return {
        "total_requests": row["total_requests"] or 0,
        "total_prompt_tokens": row["total_prompt_tokens"] or 0,
        "total_completion_tokens": row["total_completion_tokens"] or 0,
        "total_tokens": row["total_tokens"] or 0,
        "estimated_cost_usd": round(cost_usd, 6),
        "estimated_cost_cny": cost_cny,
        "avg_ttft_ms": round(float(row["avg_ttft_ms"] or 0.0), 1),
        "avg_tts_first_chunk_ms": round(float(row["avg_tts_first_chunk_ms"] or 0.0), 1),
        "avg_total_latency_ms": round(float(row["avg_total_latency_ms"] or 0.0), 1),
    }


async def get_provider_metrics_breakdown(conn: aiosqlite.Connection) -> List[Dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cur_tot = await conn.execute("SELECT COALESCE(SUM(total_tokens), 0) as grand_total FROM token_usage_metrics;")
    row_tot = await cur_tot.fetchone()
    grand_total = row_tot["grand_total"] if row_tot and row_tot["grand_total"] > 0 else 1

    cursor = await conn.execute("""
        SELECT 
            provider_id,
            COUNT(*) as request_count,
            COALESCE(SUM(total_tokens), 0) as total_tokens,
            COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
            COALESCE(SUM(completion_tokens), 0) as completion_tokens,
            COALESCE(SUM(estimated_cost), 0.0) as estimated_cost_usd
        FROM token_usage_metrics
        GROUP BY provider_id
        ORDER BY total_tokens DESC;
    """)
    rows = await cursor.fetchall()
    results = []
    
    provider_names = {
        "deepseek": "DeepSeek",
        "openai": "OpenAI",
        "gemini": "Google Gemini",
        "anthropic": "Anthropic Claude",
        "qwen": "通义千问 (Qwen)",
        "glm": "智谱 GLM",
        "xai": "xAI (Grok)",
        "siliconflow": "SiliconFlow",
        "custom": "自定义模型"
    }

    for r in rows:
        pid = r["provider_id"]
        t_tokens = r["total_tokens"] or 0
        pct = round((t_tokens / grand_total) * 100.0, 1)
        results.append({
            "provider_id": pid,
            "name": provider_names.get(pid, pid.capitalize()),
            "request_count": r["request_count"] or 0,
            "total_tokens": t_tokens,
            "prompt_tokens": r["prompt_tokens"] or 0,
            "completion_tokens": r["completion_tokens"] or 0,
            "estimated_cost_usd": round(float(r["estimated_cost_usd"] or 0.0), 6),
            "percentage": pct
        })
    return results


async def get_recent_latency_trends(conn: aiosqlite.Connection, limit: int = 30) -> List[Dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("""
        SELECT * FROM (
            SELECT 
                timestamp,
                ttft_ms,
                tts_first_chunk_ms,
                total_latency_ms,
                model_name,
                provider_id
            FROM token_usage_metrics
            ORDER BY id DESC
            LIMIT ?
        ) ORDER BY timestamp ASC;
    """, (limit,))
    rows = await cursor.fetchall()
    return [
        {
            "timestamp": r["timestamp"],
            "ttft_ms": round(float(r["ttft_ms"] or 0.0), 1),
            "tts_first_chunk_ms": round(float(r["tts_first_chunk_ms"] or 0.0), 1),
            "total_latency_ms": round(float(r["total_latency_ms"] or 0.0), 1),
            "model_name": r["model_name"] or "",
            "provider_id": r["provider_id"] or "",
        }
        for r in rows
    ]


