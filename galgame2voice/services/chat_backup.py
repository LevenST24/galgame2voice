"""Consistent, portable text backups independent of sidebar/history pagination."""

from datetime import datetime, timezone
import json
import math
import unicodedata

from fastapi import HTTPException

from galgame2voice.database.models import decode_lines
from galgame2voice.database.session import get_db
from galgame2voice.services.background_manifest import BACKGROUND_ID_RE

MAX_SESSIONS = 10000
MAX_MESSAGES = 50000
MAX_TEXT = 10_000_000
SESSION_SETTINGS = frozenset({
    "systemPrompt", "voiceProfileId", "temperature", "topP", "maxTokens",
    "freqPenalty", "presPenalty", "maxContext", "aiAdaptiveVoice", "ttsSpeed",
    "ttsTopK", "ttsTopP", "ttsTemperature", "advReplyFormat",
    "advPlayerName", "advPrimaryLanguage", "advSubtitleLanguage",
    "advWindowOpacity", "advTextIntervalMs", "advInterruptVoice", "advAutoBaseMs",
    "advAutoPerCharMs", "advAutoMode", "advOutfit", "advBlink", "advLipSync",
    "advBackground", "advBgTime",
})

_ADV_ENUMS = {
    "advPrimaryLanguage": {"zh", "ja"}, "advSubtitleLanguage": {"zh", "ja", "none"},
    "advAutoMode": {"normal", "voice-priority"}, "advReplyFormat": {"old", "lines"},
    "advBgTime": {"auto", "day", "evening", "night"},
}
_ADV_RANGES = {"advWindowOpacity": 100, "advTextIntervalMs": 100, "advAutoBaseMs": 6000, "advAutoPerCharMs": 200}
# ECMAScript trim (pickSettings), which differs from Python str.strip for BOM/NEL.
# The browser enforces the 40-grapheme player-name limit with Intl.Segmenter.
# The server only bounds the length, so it never drops a name the browser accepts.
_PLAYER_NAME_MAX_CODEPOINTS = 400
_JS_WHITESPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


def _valid_adv_setting(key: str, value) -> bool:
    if key in {"advInterruptVoice", "advBlink", "advLipSync"}:
        return type(value) is bool
    if key in _ADV_ENUMS:
        return isinstance(value, str) and value in _ADV_ENUMS[key]
    if key in _ADV_RANGES:
        # JSON numbers have one type in JS: 75.0 is an integer there, true is not.
        return type(value) in {int, float} and 0 <= value <= _ADV_RANGES[key] and math.isfinite(value) and int(value) == value
    if key == "advPlayerName":
        return isinstance(value, str) and bool(value.strip(_JS_WHITESPACE)) and len(value) <= _PLAYER_NAME_MAX_CODEPOINTS
    if key == "advOutfit":
        return isinstance(value, str) and len(value) <= 200 and all(
            char in "_-" or unicodedata.category(char)[0] in "LN" for char in value
        )
    if key == "advBackground":
        # "" (follow the character's work), "none" (plain sky) or a global "<work>/<id>" key.
        if not isinstance(value, str):
            return False
        if value in ("", "none"):
            return True
        parts = value.split("/")
        return len(parts) == 2 and all(BACKGROUND_ID_RE.fullmatch(part) for part in parts)
    return True


def validate_adv_settings(settings: dict) -> dict:
    """Drop invalid P3 values, preserving every unrelated key for the caller."""
    result = {key: value for key, value in settings.items() if key != "advEnabled" and _valid_adv_setting(key, value)}
    primary = result.get("advPrimaryLanguage", "zh")
    if result.get("advSubtitleLanguage", "ja") == primary:
        result.pop("advSubtitleLanguage", None)
    return result


def _timestamp(value: str | None) -> int:
    if not value:
        return 0
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, OverflowError, OSError):
        return 0


def import_timestamp(value: int | None) -> str:
    dt = datetime.now(timezone.utc) if value is None else datetime.fromtimestamp(value / 1000, timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _too_large() -> HTTPException:
    return HTTPException(413, "记录超过单份聊天备份的导入上限，请关闭程序后备份整个 data 文件夹；当前记录未修改。")


async def export_chat_backup() -> dict:
    async with get_db() as conn:
        # A read transaction provides one WAL snapshot without reserving a writer.
        await conn.execute("BEGIN")
        try:
            async with conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC, id DESC LIMIT ?", (MAX_SESSIONS + 1,)) as cursor:
                rows = await cursor.fetchall()
            if len(rows) > MAX_SESSIONS:
                raise _too_large()
            sessions = {}
            for row in rows:
                try:
                    settings = json.loads(row["settings_json"] or "{}")
                except (TypeError, ValueError):
                    settings = {}
                settings = {key: value for key, value in settings.items() if key in SESSION_SETTINGS} if isinstance(settings, dict) else {}
                settings = validate_adv_settings(settings)
                settings["voiceProfileId"] = row["voice_profile_id"]
                if row["custom_system_prompt"]:
                    settings["systemPrompt"] = row["custom_system_prompt"]
                sessions[row["id"]] = {
                    "id": row["id"], "title": row["title"] or "新对话", "settings": settings,
                    "createdAt": _timestamp(row["created_at"]), "updatedAt": _timestamp(row["updated_at"]),
                    "messages": [],
                }
            count = size = 0
            async with conn.execute(
                "SELECT m.id, m.session_id, m.role, m.content_chinese, m.content_japanese, m.created_at, m.lines_json "
                "FROM messages m JOIN sessions s ON s.id = m.session_id ORDER BY m.id LIMIT ?", (MAX_MESSAGES + 1,)
            ) as cursor:
                async for row in cursor:
                    count += 1
                    content, japanese = row["content_chinese"] or "", row["content_japanese"] or ""
                    size += len(content) + len(japanese) + len(row["lines_json"] or "")
                    if count > MAX_MESSAGES or size > MAX_TEXT:
                        raise _too_large()
                    messages = sessions[row["session_id"]]["messages"]
                    if len(messages) >= 10000:
                        raise _too_large()
                    raw_lines = decode_lines(row["lines_json"])
                    if raw_lines is not None:
                        lines = [
                            {
                                "index": line["index"],
                                "zh": line["zh"],
                                "ja": line["ja"],
                                "emotion": line["emotion"],
                                "act": line["act"],
                                "audio": {"status": "skipped", "chunks": []},
                            }
                            for line in raw_lines
                        ]
                    else:
                        lines = None
                    messages.append({
                        "id": f"m_{row['id']}", "role": row["role"], "content": content,
                        "japanese": japanese, "ts": _timestamp(row["created_at"]),
                        "lines": lines,
                    })
            return {
                "format": "galgame2voice.chat", "version": 1,
                "exportedAt": datetime.now(timezone.utc).isoformat(),
                "sessions": list(sessions.values()), "activeId": next(iter(sessions), None), "global": {},
            }
        finally:
            await conn.rollback()
