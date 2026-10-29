"""Keep an assistant reply attached to the exact user message that started it."""

import asyncio

import aiosqlite


class TurnUnavailableError(RuntimeError):
    """The originating conversation or user message was removed during generation."""


async def require_user_turn(conn: aiosqlite.Connection, session_id: str, user_message_id: int | None) -> None:
    """Check the turn under the same write transaction that will save its reply."""
    if getattr(conn, "_imm_tx_lock_owner", None) is not asyncio.current_task() or not getattr(conn, "_imm_tx_depth", 0):
        raise RuntimeError("require_user_turn requires the current task's immediate transaction")
    if user_message_id is not None:
        cursor = await conn.execute(
            "SELECT 1 FROM messages WHERE id = ? AND session_id = ? AND role = 'user' LIMIT 1",
            (user_message_id, session_id),
        )
        if await cursor.fetchone():
            return
    raise TurnUnavailableError("对话已被删除或清空，本次回复未保存。")
