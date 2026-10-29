"""Explicit repair of one stored or browser-local dialogue line."""

import asyncio
import copy
import json
import logging
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from galgame2voice.database import crud
from galgame2voice.database.models import TtsOptions, decode_lines
from galgame2voice.database.session import get_db, get_database_path, immediate_transaction
from galgame2voice.services.gpt_sovits_client import normalize_japanese_for_tts
from galgame2voice.services.tts_service import VoiceModelMissingError, VoiceReferenceMissingError
from galgame2voice.services.voice_resolver import VoiceContextUnavailableError
from galgame2voice.services.voice_manager import get_voice_manager
from galgame2voice.services.audio_artifacts import REPAIR_AUDIO_PREFIX, discard_repair_audio
from galgame2voice.utils.async_tasks import cancel_and_drain

logger = logging.getLogger(__name__)
router = APIRouter()


class LineAudioRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message_id: int | None = Field(default=None, ge=1)
    line_index: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=2000)
    voice_profile_id: int = Field(ge=1)
    options: TtsOptions | None = None
    emotion: str = Field(default="neutral", max_length=32)
    ai_adaptive_voice: bool = True


async def _stored_line(conn, req: LineAudioRequest):
    row = await (await conn.execute(
        "SELECT role, lines_json FROM messages WHERE id=? AND session_id=?",
        (req.message_id, req.session_id),
    )).fetchone()
    lines = decode_lines(row["lines_json"]) if row and row["role"] == "assistant" else None
    line = next((line for line in lines or [] if line["index"] == req.line_index), None)
    if line is None:
        raise HTTPException(404, "这句历史台词已不存在，请刷新聊天记录后再试。")
    if line["ja"] != req.text:
        raise HTTPException(409, "这句台词内容已变化，原音频未修改，请重新打开 LOG 后再试。")
    return lines, line


async def _synthesize_connected(request: Request, service, text, options):
    async def disconnected():
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                return

    synthesis = asyncio.create_task(service.synthesize_to_file(
        text, options=options, filename_prefix=REPAIR_AUDIO_PREFIX, use_cache=False,
    ))
    watcher = asyncio.create_task(disconnected())
    returned = False
    try:
        done, _ = await asyncio.wait({synthesis, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            raise HTTPException(499, "语音生成已取消，原音频与文字已保留。")
        result = await synthesis
        returned = True
        return result
    finally:
        try:
            await cancel_and_drain((synthesis, watcher))
        finally:
            if not returned and synthesis.done() and not synthesis.cancelled() and synthesis.exception() is None:
                await discard_repair_audio(service.audio_dir, synthesis.result()[1])


@router.post("/regenerate-line", summary="Regenerate one line with an explicitly selected voice")
async def regenerate_line(req: LineAudioRequest, request: Request, manager: Annotated[Any, Depends(get_voice_manager)]):
    service = manager.tts_service
    db_path = service.db_path or get_database_path()
    if not req.session_id.strip():
        raise HTTPException(422, "会话编号不能为空。")
    text = normalize_japanese_for_tts(req.text)
    if not text.strip() or not any(char.isalnum() for char in text):
        raise HTTPException(422, "这句没有可配音的日文台词，原记录未修改。")
    async with get_db(db_path) as conn:
        profile = await crud.get_voice_profile(conn, req.voice_profile_id)
        if not profile:
            raise HTTPException(404, "所选音色已不存在，请在角色与会话中重新选择。")
        previous_audio = None
        if req.message_id is not None:
            _, line = await _stored_line(conn, req)
            previous_audio = copy.deepcopy(line["audio"])

    options = req.options.model_dump(exclude_unset=True) if req.options else {}
    options.update(voice_profile_id=req.voice_profile_id, emotion=req.emotion,
                   ai_adaptive_voice=req.ai_adaptive_voice, _priority=0)
    try:
        url, path, size = await _synthesize_connected(request, service, text, options)
    except HTTPException:
        raise
    except VoiceModelMissingError as exc:
        raise HTTPException(422, "所选音色缺少模型权重，请在设置中心的角色与会话中补全模型路径。原音频与文字已保留。") from exc
    except VoiceReferenceMissingError as exc:
        raise HTTPException(422, "所选音色缺少参考音频，请在设置中心的角色与会话中补全参考音频。原音频与文字已保留。") from exc
    except VoiceContextUnavailableError as exc:
        raise HTTPException(503, "音色配置暂时无法读取，请稍后重试。原音频与文字已保留。") from exc
    except Exception as exc:
        logger.warning("Single-line audio regeneration failed: %s", type(exc).__name__)
        raise HTTPException(502, "这句语音暂时未能重新生成，原音频与文字已保留。请在设置中心的运行状态检查语音引擎后重试。") from exc
    published = write_started = False
    try:
        if size <= 0:
            raise HTTPException(502, "生成的语音为空，原音频与文字已保留。请检查语音引擎后重试。")
        if await request.is_disconnected():
            raise HTTPException(499, "语音生成已取消，原音频与文字已保留。")
        audio = {
            "status": "ready", "generated_at": int(time.time() * 1000),
            "voice_profile_id": req.voice_profile_id, "voice_name": profile.name,
            "chunks": [{"index": 0, "line": req.line_index, "audio_url": url, "sentence": req.text}],
        }
        if req.message_id is not None:
            async with get_db(db_path) as conn:
                async with immediate_transaction(conn):
                    lines, line = await _stored_line(conn, req)
                    if line["audio"] != previous_audio:
                        raise HTTPException(409, "这句已有其他音频修复结果，未覆盖现有音频，请重新打开 LOG 检查。")
                    if await request.is_disconnected():
                        raise HTTPException(499, "语音生成已取消，原音频与文字已保留。")
                    line["audio"] = audio
                    # From this point a cancellation may race a durable commit.
                    # Keep the file even if the response cannot confirm the write.
                    write_started = True
                    await conn.execute("UPDATE messages SET lines_json=?, audio_url='' WHERE id=? AND session_id=?",
                                       (json.dumps(lines, ensure_ascii=False), req.message_id, req.session_id))
        published = True
        return {"session_id": req.session_id, "message_id": req.message_id, "line_index": req.line_index,
                "voice_profile_id": req.voice_profile_id, "audio": audio, "persisted": req.message_id is not None}
    finally:
        if not published and not write_started:
            await discard_repair_audio(service.audio_dir, path)
