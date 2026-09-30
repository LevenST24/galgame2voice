"""
SQLite Schema Migrations and Version Management for galgame2voice.
Decouples DDL table definitions, indexing, seed initialization, and version upgrades
from runtime CRUD operations.
"""

import logging
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any
import aiosqlite

from galgame2voice.database.session import (
    get_schema_version,
    set_schema_version,
)
from galgame2voice.security.crypto import encrypt_secret, is_encrypted_secret

logger = logging.getLogger("galgame2voice.database.migrations")

# Default reference audio ships inside the repo using portable relative paths.
# Converted to absolute at the client boundary when dispatching to GPT-SoVITS.
_DEFAULT_REF_AUDIO = "audio/references/natsume/gentle.ogg"
_DEFAULT_REF_TEXT = "とりあえず、今日見たことは忘れて、わかった?"

# ==================== Schema Versioning & Migrations ====================

CURRENT_SCHEMA_VERSION = 6


async def _get_table_columns(conn: aiosqlite.Connection, table_name: str) -> set[str]:
    """Returns set of column names for an existing SQLite table."""
    try:
        cursor = await conn.execute(f"PRAGMA table_info({table_name});")
        rows = await cursor.fetchall()
        return {r["name"] for r in rows}
    except Exception:
        return set()


async def _add_column_if_missing(
    conn: aiosqlite.Connection, table_name: str, column_name: str, column_type: str
) -> bool:
    """Idempotently adds a column to a table if it does not already exist."""
    existing = await _get_table_columns(conn, table_name)
    if column_name not in existing:
        await conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type};")
        return True
    return False


def _save_console_token_file(token: str) -> None:
    """Securely writes console token to data/.console_token (0600 permissions)."""
    try:
        from galgame2voice.config import get_settings
        token_file = get_settings().data_dir / ".console_token"
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token.strip(), encoding="utf-8")
        try:
            os.chmod(token_file, 0o600)
        except OSError:
            pass
        masked = token[:4] + "****" + token[-4:]
        logger.warning(
            "Saved console access token to data/.console_token (masked: %s). "
            "Override anytime via GALGAME2VOICE_CONSOLE_TOKEN env var.",
            masked,
        )
    except Exception as exc:
        logger.warning("Could not write console token to file: %s", exc)


async def _migration_v1_base_schema(conn: aiosqlite.Connection) -> None:
    """Migration 1: Base table creation, primary indexes, and initial seeds."""
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
            telegram_enabled INTEGER NOT NULL DEFAULT 0,
            console_token TEXT NOT NULL DEFAULT '',
            console_url TEXT NOT NULL DEFAULT '',
            max_history_messages INTEGER NOT NULL DEFAULT 10,
            inference_precision TEXT NOT NULL DEFAULT 'auto',
            stt_engine TEXT NOT NULL DEFAULT 'browser',
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
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_voice_profiles_is_default ON voice_profiles(is_default);")

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            channel TEXT NOT NULL DEFAULT 'web',
            user_id TEXT NOT NULL DEFAULT '',
            voice_profile_id INTEGER REFERENCES voice_profiles(id) ON DELETE SET NULL,
            custom_system_prompt TEXT DEFAULT NULL,
            title TEXT NOT NULL DEFAULT '',
            settings_json TEXT DEFAULT NULL,
            token_budget INTEGER NOT NULL DEFAULT 4096,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_channel ON sessions(channel);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_voice_profile ON sessions(voice_profile_id);")

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
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_created ON messages(session_id, created_at);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_role_session ON messages(session_id, role);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_id_desc ON messages(session_id, id DESC);")

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
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_token_usage_provider_tokens ON token_usage_metrics(provider_id, total_tokens);")

    # Seed Voice Profile 1
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

重要：你必须严格输出如下 JSON 格式，在最开头根据语境动态决定语音推理参数（speed 语速: 0.5~1.5 请大胆调节！激动时可设为1.3以上，低落时设为0.7以下, temp 温度: 0.60~1.20, emotion 情绪: gentle|shy|happy|tsundere|cool|sad|angry），不要输出任何多余文字、不要加代码块标记：
{"tts": {"speed": 1.05, "temp": 0.95, "emotion": "gentle"}, "chinese": "显示给玩家的中文台词", "japanese": "对应的口语化日文台词"}

要求：
1. tts 包含 speed 语速、temp 生成温度、emotion 情绪，在开头根据每句话的情境动态微调。
2. chinese 是给中文玩家看的内容；japanese 是同样含义的日文，口语自然、适合配音。
3. japanese 必须符合四季夏目的角色口吻。
4. 字段都不能为空。
5. 始终以四季夏目的身份回复，不要解释设定、不要跳出角色。"""
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
            _DEFAULT_REF_AUDIO,
            _DEFAULT_REF_TEXT,
            "ja",
            "ja",
            natsume_prompt,
            1
        ))

    # Seed Providers
    cursor = await conn.execute("SELECT COUNT(*) FROM providers;")
    count_row = await cursor.fetchone()
    count = count_row[0] if count_row else 0
    if count == 0:
        presets = [
            ("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.7-flash", "", 1),
            ("openai", "OpenAI", "https://api.openai.com/v1", "gpt-5.6-sol", "whisper-1", 0),
            ("deepseek", "DeepSeek", "https://api.deepseek.com", "deepseek-v4-pro", "", 0),
            ("anthropic", "Anthropic Claude", "https://api.anthropic.com/v1", "claude-5-sonnet-latest", "", 0),
            ("xai", "xAI (Grok)", "https://api.x.ai/v1", "grok-3", "", 0),
            ("glm", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-5.3", "", 0),
            ("qwen", "通义千问 (Qwen)", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.8-max", "qwen-audio-asr", 0),
            ("custom", "自定义 / 本地模型 (Ollama / vLLM)", "http://127.0.0.1:11434/v1", "deepseek-v4:latest", "", 0),
        ]
        for p in presets:
            await conn.execute("""
                INSERT OR IGNORE INTO providers (id, name, api_base_url, api_key, chat_model, stt_model, is_active, custom_headers)
                VALUES (?, ?, ?, '', ?, ?, ?, '{}');
            """, p)

    # Seed Settings
    cursor = await conn.execute("SELECT COUNT(*) FROM settings WHERE id = 1;")
    count_row = await cursor.fetchone()
    count = count_row[0] if count_row else 0
    if count == 0:
        token = uuid.uuid4().hex
        _save_console_token_file(token)
        encrypted_token = encrypt_secret(token)
        await conn.execute("""
            INSERT OR IGNORE INTO settings (
                id, active_provider_id, active_voice_profile_id, gpt_sovits_url,
                audio_output_dir, audio_retention_minutes, audio_cleanup_interval_sec,
                speed_factor, temperature, top_k, top_p, seed, batch_size,
                text_split_method, fragment_interval, telegram_bot_token,
                telegram_bot_username, telegram_proxy_host, telegram_proxy_port,
                telegram_proxy_enabled, telegram_enabled, console_token, console_url, max_history_messages
            ) VALUES (
                1, 'deepseek', 1, 'http://127.0.0.1:9880',
                'audio', 30, 600,
                1.0, 1.0, 15, 1.0, -1, 1,
                'cut1', 0.3, '',
                'natsume_siki_bot', '127.0.0.1', 10809,
                0, 0, ?, '', 10
            );
        """, (encrypted_token,))


async def _migration_v2_columns(conn: aiosqlite.Connection) -> None:
    """Migration 2: Ensure providers and voice_profiles tables have all required columns."""
    for col, col_type in [
        ("name", "TEXT NOT NULL DEFAULT ''"),
        ("api_base_url", "TEXT NOT NULL DEFAULT ''"),
        ("chat_model", "TEXT NOT NULL DEFAULT ''"),
        ("stt_model", "TEXT NOT NULL DEFAULT ''"),
        ("custom_headers", "TEXT NOT NULL DEFAULT '{}'"),
    ]:
        await _add_column_if_missing(conn, "providers", col, col_type)

    for col, col_type in [
        ("description", "TEXT NOT NULL DEFAULT ''"),
        ("system_prompt", "TEXT NOT NULL DEFAULT ''"),
        ("prompt_lang", "TEXT NOT NULL DEFAULT 'ja'"),
        ("text_lang", "TEXT NOT NULL DEFAULT 'ja'"),
        ("ref_audio_path", "TEXT NOT NULL DEFAULT ''"),
        ("prompt_text", "TEXT NOT NULL DEFAULT ''"),
    ]:
        await _add_column_if_missing(conn, "voice_profiles", col, col_type)


async def _migration_v3_security_and_indexes(conn: aiosqlite.Connection) -> None:
    """Migration 3: Settings security columns, memory uniqueness, and composite query indexes."""
    for col, col_type in [
        ("telegram_admin_ids", "TEXT NOT NULL DEFAULT ''"),
        ("allow_private_llm_endpoints", "INTEGER NOT NULL DEFAULT 0"),
        ("inference_precision", "TEXT NOT NULL DEFAULT 'auto'"),
        ("telegram_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("stt_engine", "TEXT NOT NULL DEFAULT 'browser'"),
    ]:
        await _add_column_if_missing(conn, "settings", col, col_type)

    # user_memories uniqueness cleanup and index
    try:
        await conn.execute("UPDATE user_memories SET character_id = 1 WHERE character_id IS NULL;")
        await conn.execute("""
            DELETE FROM user_memories
            WHERE id NOT IN (
                SELECT MAX(id) FROM user_memories
                GROUP BY user_id, character_id, fact_key
            );
        """)
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_memories_key ON user_memories(user_id, character_id, fact_key);"
        )
    except Exception as exc:
        logger.debug("user_memories index migration skipped or already satisfied: %s", exc)

    # Composite query indexes
    try:
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_created ON messages(session_id, created_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_id_desc ON messages(session_id, id DESC);")
    except Exception as exc:
        logger.debug("Composite index creation skipped: %s", exc)


async def _migration_v4_prompts_and_self_healing(conn: aiosqlite.Connection) -> None:
    """Migration 4: Upgrade legacy default voice profile prompt with dynamic TTS parameters and self-heal audios."""
    try:
        cur = await conn.execute("SELECT id, system_prompt FROM voice_profiles WHERE is_default = 1 OR id = 1;")
        rows = await cur.fetchall()
        for row in rows:
            if not row or not row["system_prompt"]:
                continue
            curr_prompt = row["system_prompt"]
            if '"tts":' not in curr_prompt and '{"chinese":' in curr_prompt:
                new_prompt = curr_prompt.replace(
                    '{"chinese": "显示给玩家的中文台词", "japanese": "对应的口语化日文台词"}',
                    '{"tts": {"speed": 1.05, "temp": 0.95, "emotion": "gentle"}, "chinese": "显示给玩家的中文台词", "japanese": "对应的口语化日文台词"}'
                )
                if "动态决定语音推理参数" not in new_prompt:
                    new_prompt = new_prompt.replace(
                        "你必须严格输出如下 JSON 格式",
                        "你必须严格输出如下 JSON 格式，在最开头根据语境动态决定语音推理参数（speed 语速: 0.5~1.5 请大胆调节！激动时可设为1.3以上，低落时设为0.7以下, temp 温度: 0.60~1.20, emotion 情绪: gentle|shy|happy|tsundere|cool|sad|angry）"
                    )
                await conn.execute("UPDATE voice_profiles SET system_prompt = ? WHERE id = ?;", (new_prompt, row["id"]))
            elif '请大胆调节！' not in curr_prompt and '动态决定语音推理参数' in curr_prompt:
                new_prompt = curr_prompt.replace(
                    "speed 语速: 0.70~1.35",
                    "speed 语速: 0.5~1.5 请大胆调节！激动时可设为1.3以上，低落时设为0.7以下"
                )
                await conn.execute("UPDATE voice_profiles SET system_prompt = ? WHERE id = ?;", (new_prompt, row["id"]))
    except Exception as exc:
        logger.debug("Could not auto-upgrade default voice profile system prompt: %s", exc)

    # Ensure cache indexes
    try:
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_tts_cache_last_accessed ON tts_cache_entries(last_accessed_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_tts_cache_clean_text ON tts_cache_entries(clean_text);")
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        pass


async def _migration_v5_precision_and_column_integrity(conn: aiosqlite.Connection) -> None:
    """Migration 5: Ensure settings table has inference_precision and all security/runtime columns."""
    for col, col_type in [
        ("inference_precision", "TEXT NOT NULL DEFAULT 'auto'"),
        ("telegram_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("telegram_admin_ids", "TEXT NOT NULL DEFAULT ''"),
        ("allow_private_llm_endpoints", "INTEGER NOT NULL DEFAULT 0"),
        ("stt_engine", "TEXT NOT NULL DEFAULT 'browser'"),
    ]:
        await _add_column_if_missing(conn, "settings", col, col_type)


async def _migration_v6_session_titles_and_settings(conn: aiosqlite.Connection) -> None:
    """Migration 6: Ensure sessions table has title and settings_json columns."""
    await _add_column_if_missing(conn, "sessions", "title", "TEXT NOT NULL DEFAULT ''")
    await _add_column_if_missing(conn, "sessions", "settings_json", "TEXT DEFAULT NULL")
    try:
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_id_desc ON messages(session_id, id DESC);")
    except Exception as exc:
        logger.debug("idx_messages_session_id_desc index creation skipped: %s", exc)


async def run_schema_migrations(conn: aiosqlite.Connection) -> int:
    """
    Executes SQLite schema migrations idempotently using PRAGMA user_version.
    Guarantees that databases upgrade safely without losing any user data.
    """
    current_version = await get_schema_version(conn)

    if current_version < 1:
        await _migration_v1_base_schema(conn)
        current_version = 1
        await set_schema_version(conn, 1)

    if current_version < 2:
        await _migration_v2_columns(conn)
        current_version = 2
        await set_schema_version(conn, 2)

    if current_version < 3:
        await _migration_v3_security_and_indexes(conn)
        current_version = 3
        await set_schema_version(conn, 3)

    if current_version < 4:
        await _migration_v4_prompts_and_self_healing(conn)
        current_version = 4
        await set_schema_version(conn, 4)

    if current_version < 5:
        await _migration_v5_precision_and_column_integrity(conn)
        current_version = 5
        await set_schema_version(conn, 5)

    if current_version < 6:
        await _migration_v6_session_titles_and_settings(conn)
        current_version = 6
        await set_schema_version(conn, 6)

    return current_version


async def init_schema_and_seeds(conn: aiosqlite.Connection) -> None:
    """Create tables, indexes, apply schema version migrations, and guarantee credentials."""
    conn.row_factory = aiosqlite.Row
    await run_schema_migrations(conn)

    # Always ensure settings columns exist even on existing databases with legacy schema history
    for col, col_type in [
        ("inference_precision", "TEXT NOT NULL DEFAULT 'auto'"),
        ("telegram_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("telegram_admin_ids", "TEXT NOT NULL DEFAULT ''"),
        ("allow_private_llm_endpoints", "INTEGER NOT NULL DEFAULT 0"),
        ("stt_engine", "TEXT NOT NULL DEFAULT 'browser'"),
    ]:
        await _add_column_if_missing(conn, "settings", col, col_type)

    # Always ensure sessions columns exist even on legacy databases
    await _add_column_if_missing(conn, "sessions", "title", "TEXT NOT NULL DEFAULT ''")
    await _add_column_if_missing(conn, "sessions", "settings_json", "TEXT DEFAULT NULL")

    # Composite query index for recent message history retrieval
    try:
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_id_desc ON messages(session_id, id DESC);")
    except Exception as exc:
        logger.debug("idx_messages_session_id_desc index creation skipped: %s", exc)

    # 4. Transparent migration: encrypt existing plaintext credentials at rest
    try:
        cursor = await conn.execute("SELECT id, api_key FROM providers WHERE api_key IS NOT NULL AND api_key != '';")
        p_rows = await cursor.fetchall()
        for p_row in p_rows:
            p_id, p_key = p_row["id"], p_row["api_key"]
            if p_key and not is_encrypted_secret(p_key):
                await conn.execute("UPDATE providers SET api_key = ? WHERE id = ?;", (encrypt_secret(p_key), p_id))

        cursor = await conn.execute("SELECT telegram_bot_token, console_token FROM settings WHERE id = 1;")
        s_row = await cursor.fetchone()
        if s_row:
            tg_token = s_row["telegram_bot_token"]
            c_token = s_row["console_token"]
            if tg_token and not is_encrypted_secret(tg_token):
                await conn.execute("UPDATE settings SET telegram_bot_token = ? WHERE id = 1;", (encrypt_secret(tg_token),))
            if c_token and not is_encrypted_secret(c_token):
                await conn.execute("UPDATE settings SET console_token = ? WHERE id = 1;", (encrypt_secret(c_token),))
    except Exception as exc:
        logger.debug("Credentials encryption migration step skipped: %s", exc)

    # Guarantee a console token exists so the API is never left unauthenticated.
    # The token is saved encrypted in the DB, and written to data/.console_token (0600).
    # Logs NEVER print the complete plaintext token (only masked).
    try:
        cursor = await conn.execute("SELECT console_token FROM settings WHERE id = 1;")
        row = await cursor.fetchone()
        if row is not None and not str(row["console_token"] or "").strip():
            new_token = uuid.uuid4().hex
            _save_console_token_file(new_token)
            encrypted_token = encrypt_secret(new_token)
            await conn.execute(
                "UPDATE settings SET console_token = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1;",
                (encrypted_token,),
            )
    except Exception as exc:
        logger.debug("Could not auto-generate missing console token in initialize_database: %s", exc)

    # Auto-heal missing or broken reference audio paths across existing voice profiles
    try:
        await auto_heal_voice_profiles(conn)
    except Exception as exc:
        logger.debug("Auto-heal voice profiles during schema init skipped: %s", exc)

    await conn.commit()


async def auto_heal_voice_profiles(conn: aiosqlite.Connection, char_mgr: Any | None = None) -> int:
    """
    Scans voice_profiles table and auto-heals any missing or invalid reference audio paths.
    If ref_audio_path points to a non-existent file or an unresolvable path,
    it automatically updates the path to a verified existing bundled reference audio file.
    Returns the number of healed profiles.
    """
    from galgame2voice.config import get_settings
    settings = get_settings()
    project_root = settings.project_root

    char_gentle = project_root / "characters" / "四季夏目" / "refs" / "gentle.ogg"
    bundled_nat = project_root / "audio" / "nat002_032.ogg"
    bundled_gentle = project_root / "audio" / "references" / "natsume" / "gentle.ogg"

    default_ref_path = "audio/references/natsume/gentle.ogg"
    if not bundled_gentle.is_file():
        if char_gentle.is_file():
            default_ref_path = "characters/四季夏目/refs/gentle.ogg"
        elif bundled_nat.is_file():
            default_ref_path = "audio/nat002_032.ogg"

    if char_mgr is not None:
        try:
            def_pkg = char_mgr.get_default_character()
            if def_pkg and getattr(def_pkg, "manifest", None):
                emotions = getattr(def_pkg.manifest, "emotions", None) or {}
                cand_emo = emotions.get("gentle") or (next(iter(emotions.values())) if emotions else None)
                if cand_emo and getattr(cand_emo, "audio", None):
                    folder = getattr(def_pkg, "folder_path", None) or getattr(def_pkg, "folder", None)
                    if folder:
                        cand = folder / cand_emo.audio
                        if cand.is_file():
                            default_ref_path = cand.resolve().relative_to(project_root.resolve()).as_posix()
            else:
                default_ref_path = ""
        except Exception:
            default_ref_path = ""

    cursor = await conn.execute("SELECT id, name, ref_audio_path FROM voice_profiles;")
    rows = await cursor.fetchall()
    healed_count = 0

    for row in rows:
        p_id = row["id"]
        ref_path = str(row["ref_audio_path"] or "").strip()
        needs_healing = False
        new_ref_path = default_ref_path

        if not ref_path:
            needs_healing = True
        else:
            p = Path(ref_path)
            file_exists = False
            try:
                root_resolved = project_root.resolve()
                audio_dir_resolved = settings.audio_dir.resolve()
                if p.is_absolute():
                    if p.is_file():
                        resolved = p.resolve()
                        if resolved.is_relative_to(root_resolved):
                            rel_posix = resolved.relative_to(root_resolved).as_posix()
                            if rel_posix != ref_path:
                                new_ref_path = rel_posix
                                needs_healing = True
                            file_exists = True
                        elif resolved.is_relative_to(audio_dir_resolved):
                            file_exists = True
                    # Machine-specific absolute paths outside project root or audio_dir
                    # do not resolve cleanly and are healed for cross-machine portability.
                else:
                    from galgame2voice.utils.path_guard import resolve_existing_audio_path
                    if resolve_existing_audio_path(ref_path) is not None:
                        file_exists = True
            except Exception:
                file_exists = False

            if not file_exists:
                needs_healing = True

        if needs_healing and new_ref_path:
            logger.info(
                "Auto-healing voice profile %d ('%s'): invalid ref_audio_path '%s' -> '%s'",
                p_id, row["name"], ref_path, new_ref_path
            )
            await conn.execute(
                "UPDATE voice_profiles SET ref_audio_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                (new_ref_path, p_id)
            )
            healed_count += 1

    return healed_count
