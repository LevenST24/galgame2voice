"""
Long-term Memory & RAG Retrieval Engine for galgame2voice.
Provides heuristic regex fact extraction, memory deduplication,
dynamic relevance scoring, anchor retrieval, and prompt injection.
"""

import logging
import re
import time
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Union, Tuple
from pathlib import Path
import aiosqlite

from galgame2voice.database import crud
from galgame2voice.database.models import (
    UserMemoryCreate, UserMemoryUpdate, UserMemoryResponse, CharacterAffectionUpdate
)
from galgame2voice.database.session import get_database_path, get_db

logger = logging.getLogger("galgame2voice.services.memory_service")


class MemoryService:
    """
    Manages long-term conversational fact extraction, SQLite persistence,
    and dynamic Top-K RAG retrieval.
    """

    # Regex patterns for deterministic real-time fact extraction
    EXTRACTION_PATTERNS = [
        # 1. Nickname / Name
        {
            "category": "nickname",
            "key": "player_name",
            "regex": re.compile(r'(?:我叫|你可以叫我|我的名字是|叫我)\s*([^\s,，。！!？?~]+?)(?:(?:就好|吧|啊|呀)?(?:[。，！!？?~\s]|$))', re.IGNORECASE),
            "confidence": 1.0,
        },
        # 2. Preferences (Likes)
        {
            "category": "preference",
            "key_prefix": "like_",
            "regex": re.compile(r'(?:我喜欢|我超爱|我的爱好是|我平时喜欢|我最喜欢)\s*([^\s,，。！!？?~]+)', re.IGNORECASE),
            "confidence": 0.95,
        },
        # 3. Dislikes / Taboos
        {
            "category": "taboo",
            "key_prefix": "dislike_",
            "regex": re.compile(r'(?:我不喜欢|我讨厌|我吃不了|我受不了|我最讨厌)\s*([^\s,，。！!？?~]+)', re.IGNORECASE),
            "confidence": 0.95,
        },
        # 4. Promises / Appointments
        {
            "category": "promise",
            "key_prefix": "promise_",
            "regex": re.compile(r'(?:我们约定|答应我|这周末一起|下次一定要|以后一起)\s*([^\s,，。！!？?~]+)', re.IGNORECASE),
            "confidence": 0.9,
        },
        # 5. Identity / Occupation
        {
            "category": "identity",
            "key": "occupation",
            "regex": re.compile(r'(?:我是(?:一名)?)\s*(程序员|学生|老师|医生|工程师|店员|同班同学|店长|前辈|后辈|作家|画师|设计师|社畜|大学生|高三生)', re.IGNORECASE),
            "confidence": 1.0,
        },
    ]

    def __init__(self, db_path: Optional[Union[str, Path]] = None):
        self.db_path = str(db_path) if db_path is not None else get_database_path()

    def extract_facts_heuristic(self, text: str) -> List[Dict[str, Any]]:
        """
        Extracts structured facts from raw user input using fast deterministic regex.
        Returns a list of dicts: [{'category': ..., 'fact_key': ..., 'fact_value': ..., 'confidence': ...}]
        """
        if not text:
            return []

        facts: List[Dict[str, Any]] = []
        cleaned = text.strip()

        for pat in self.EXTRACTION_PATTERNS:
            match = pat["regex"].search(cleaned)
            if match:
                val = match.group(1).strip()
                if not val:
                    continue

                if "key" in pat:
                    key = pat["key"]
                else:
                    # Sanitize key name
                    sanitized_val = re.sub(r'[^\w\u4e00-\u9fff]', '', val)[:10]
                    key = f"{pat.get('key_prefix', 'fact_')}{sanitized_val}"

                facts.append({
                    "category": pat["category"],
                    "fact_key": key,
                    "fact_value": val,
                    "confidence": pat["confidence"],
                })

        return facts

    async def process_user_message(
        self,
        user_id: str,
        character_id: Optional[int],
        message_text: str,
        source_message_id: Optional[int] = None,
        conn: Optional[aiosqlite.Connection] = None,
    ) -> List[UserMemoryResponse]:
        """
        Extracts facts from message, stores or updates them in SQLite,
        and synchronizes custom nickname if recognized.
        """
        extracted = self.extract_facts_heuristic(message_text)
        if not extracted:
            return []

        async def _save_with_conn(db_conn: aiosqlite.Connection) -> List[UserMemoryResponse]:
            saved_list: List[UserMemoryResponse] = []
            for item in extracted:
                mem_create = UserMemoryCreate(
                    user_id=user_id,
                    character_id=character_id,
                    category=item["category"],
                    fact_key=item["fact_key"],
                    fact_value=item["fact_value"],
                    confidence=item["confidence"],
                    source_message_id=source_message_id,
                )
                saved = await crud.upsert_memory(db_conn, mem_create)
                saved_list.append(saved)

                # If nickname learned, update character affection custom_nickname
                if item["fact_key"] == "player_name" and character_id:
                    await crud.update_character_affection(
                        db_conn,
                        user_id=user_id,
                        character_id=character_id,
                        updates=CharacterAffectionUpdate(custom_nickname=item["fact_value"]),
                    )
            return saved_list

        if conn is not None:
            return await _save_with_conn(conn)

        async with get_db(self.db_path) as local_conn:
            return await _save_with_conn(local_conn)

    def _calculate_overlap_score(self, prompt: str, fact_value: str, fact_key: str) -> float:
        """
        Calculates character-level and word overlap score between prompt and memory.
        """
        if not prompt or not fact_value:
            return 0.0

        p_lower = prompt.lower()
        v_lower = fact_value.lower()

        # Exact substring match
        if v_lower in p_lower:
            return 1.0

        # Character set overlap for CJK
        fact_chars = set(v_lower)
        overlap_chars = fact_chars.intersection(set(p_lower))
        if not fact_chars:
            return 0.0

        char_score = len(overlap_chars) / len(fact_chars)

        # Keyword key match
        if fact_key.replace("like_", "").replace("dislike_", "").replace("promise_", "") in p_lower:
            char_score = max(char_score, 0.8)

        return min(1.0, char_score)

    async def retrieve_relevant_memories(
        self,
        user_id: str = "default_user",
        character_id: Optional[int] = 1,
        prompt: str = "",
        top_k: int = 5,
        conn: Optional[aiosqlite.Connection] = None,
    ) -> List[UserMemoryResponse]:
        """
        Retrieves Top-K relevant memories using Anchor Priority + Dynamic Composite Scoring.
        Anchors (nickname, identity) are always prioritized.
        """
        if conn is not None:
            all_memories = await crud.list_memories(
                conn, user_id=user_id, character_id=character_id, limit=200
            )
        else:
            async with get_db(self.db_path) as local_conn:
                all_memories = await crud.list_memories(
                    local_conn, user_id=user_id, character_id=character_id, limit=200
                )

        if not all_memories:
            return []

        # 1. Separate Anchors from Contextual Memories
        anchor_memories: List[UserMemoryResponse] = []
        candidate_memories: List[UserMemoryResponse] = []

        for m in all_memories:
            if m.category in ("nickname", "identity") or m.fact_key in ("player_name", "occupation"):
                anchor_memories.append(m)
            else:
                candidate_memories.append(m)

        # 2. Score candidate memories
        scored_candidates: List[Tuple[float, UserMemoryResponse]] = []
        now_ts = time.time()

        for m in candidate_memories:
            overlap = self._calculate_overlap_score(prompt, m.fact_value, m.fact_key)
            confidence = m.confidence

            # Recency factor
            recency = 0.5
            if m.updated_at:
                try:
                    dt = datetime.fromisoformat(m.updated_at.replace("Z", "+00:00"))
                    diff_hours = max(0.0, (now_ts - dt.timestamp()) / 3600.0)
                    recency = 1.0 / (1.0 + diff_hours / 24.0)
                except Exception:
                    recency = 0.5

            composite_score = (0.5 * overlap) + (0.3 * recency) + (0.2 * confidence)
            scored_candidates.append((composite_score, m))

        # Sort candidates descending by score
        scored_candidates.sort(key=lambda x: x[0], reverse=True)

        # 3. Combine anchors and top candidates up to top_k
        selected: List[UserMemoryResponse] = []
        seen_keys = set()

        for m in anchor_memories:
            if m.fact_key not in seen_keys:
                selected.append(m)
                seen_keys.add(m.fact_key)
                if len(selected) >= top_k:
                    break

        for score, m in scored_candidates:
            if len(selected) >= top_k:
                break
            if m.fact_key not in seen_keys:
                selected.append(m)
                seen_keys.add(m.fact_key)

        # 4. Asynchronously record recall timestamp & count
        try:
            if conn is not None:
                for sel in selected:
                    await crud.record_memory_recall(conn, sel.id)
            else:
                async with get_db(self.db_path) as local_conn:
                    for sel in selected:
                        await crud.record_memory_recall(local_conn, sel.id)
        except Exception as e:
            logger.warning("Failed to record memory recall count: %s", e)

        return selected

    def format_memory_prompt_block(
        self,
        memories: List[UserMemoryResponse],
        affection_info: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Formats retrieved memories and affection status into a structured prompt block.
        """
        blocks: List[str] = []

        if memories:
            lines = ["【角色长程记忆（关于玩家的事实与约定）】"]
            for m in memories:
                if m.category == "nickname" or m.fact_key == "player_name":
                    lines.append(f"- 玩家称呼：{m.fact_value}")
                elif m.category == "identity" or m.fact_key == "occupation":
                    lines.append(f"- 玩家身份：{m.fact_value}")
                elif m.category == "preference":
                    lines.append(f"- 玩家喜好：{m.fact_value}")
                elif m.category == "taboo":
                    lines.append(f"- 玩家忌口/讨厌：{m.fact_value}")
                elif m.category == "promise":
                    lines.append(f"- 重要约定：{m.fact_value}")
                else:
                    lines.append(f"- 记忆记录（{m.category}）：{m.fact_value}")
            lines.append("（请在对话中自然体现上述记忆，展现你一直记着玩家的事情，切勿生硬复述。）")
            blocks.append("\n".join(lines))

        if affection_info:
            lvl = affection_info.get("level", 1)
            lvl_name = affection_info.get("level_name", "初识/生疏")
            emotion = affection_info.get("emotion", "normal")
            nickname = affection_info.get("nickname")

            aff_lines = ["【当前关系与好感度】"]
            aff_lines.append(f"- 亲密度等级：Lv.{lvl} ({lvl_name})")
            aff_lines.append(f"- 当前情绪状态：{emotion}")
            if nickname:
                aff_lines.append(f"- 称呼玩家为：{nickname}")
            aff_lines.append("（请依据好感度等级和当前情绪，自然呈现对应的语气与亲密程度。）")
            blocks.append("\n".join(aff_lines))

        return "\n\n".join(blocks)


__all__ = ["MemoryService"]
