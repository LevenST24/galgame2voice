"""
Chat Service and Streaming Bilingual Pipeline for galgame2voice.
Coordinates LLM streaming, incremental JSON bilingual parsing,
sentence boundary splitting, and low-latency TTS audio generation.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Union
from pathlib import Path

import aiosqlite

from galgame2voice.config import get_settings
from galgame2voice.adapters.base import ChatMessage, BaseLLMAdapter
from galgame2voice.adapters.registry import get_llm_adapter
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_db
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.session_manager import SessionManager
from galgame2voice.utils.text_splitter import split_japanese_sentences

logger = logging.getLogger("galgame2voice.services.chat_service")


# ============================================================================
# Streaming Bilingual Parser
# ============================================================================

class StreamingBilingualParser:
    """
    Incremental state machine for parsing streaming LLM output tokens into
    immediate Chinese delta text and completed Japanese sentence chunks.
    Robust against markdown code fences, unescaped characters, partial JSON tokens,
    Unicode escape sequences split across chunks, and non-JSON fallback text.
    """

    def __init__(self):
        self.buffer: str = ""
        self.chinese_extracted: str = ""
        self.japanese_extracted: str = ""
        self.emitted_chinese_len: int = 0
        self.emitted_japanese_len: int = 0
        self.is_plain_text_fallback: bool = False

    def clean_markdown_delimiters(self, text: str) -> str:
        """Strips markdown ```json and ``` code block wrappers."""
        cleaned = re.sub(r'```(?:json)?\s*', '', text, flags=re.IGNORECASE)
        cleaned = re.sub(r'^`\s*', '', cleaned, flags=re.MULTILINE)
        return cleaned

    def _strip_incomplete_escape(self, s: str) -> str:
        """Strips trailing incomplete unicode/backslash escape sequences."""
        match = re.search(r'\\(?:u[0-9a-fA-F]{0,3}|[a-zA-Z\\]?)$', s)
        if match:
            return s[:match.start()]
        return s

    def _unescape_json_string(self, raw_str: str) -> str:
        """Safely unescapes raw JSON string fragment."""
        safe_str = self._strip_incomplete_escape(raw_str)
        try:
            return json.loads(f'"{safe_str}"')
        except Exception:
            return (
                safe_str.replace('\\"', '"')
                .replace('\\n', '\n')
                .replace('\\t', '\t')
                .replace('\\r', '\r')
                .replace('\\\\', '\\')
            )

    def feed_chunk(self, chunk: str) -> Tuple[str, List[str]]:
        """
        Feeds an incoming stream token chunk.
        Returns:
            (new_chinese_delta, list_of_newly_completed_japanese_sentences)
        """
        if not chunk:
            return "", []

        self.buffer += chunk
        sanitized = self.clean_markdown_delimiters(self.buffer)

        new_chinese_delta = ""
        new_sentences: List[str] = []

        # 1. Incremental Chinese Extraction
        # Look for "chinese" : "..."
        ch_match = re.search(r'"chinese"\s*:\s*"((?:[^"\\]|\\.)*)', sanitized)
        if ch_match:
            raw_ch = ch_match.group(1)
            current_ch = self._unescape_json_string(raw_ch)

            if len(current_ch) > self.emitted_chinese_len:
                new_chinese_delta = current_ch[self.emitted_chinese_len:]
                self.chinese_extracted = current_ch
                self.emitted_chinese_len = len(current_ch)
        else:
            # Fallback check: If the stream does not look like JSON after some tokens
            if not self.chinese_extracted and len(sanitized) > 15 and not sanitized.lstrip().startswith("{"):
                self.is_plain_text_fallback = True
                # Check for "中文：" or "中文:"
                ch_fallback = re.search(r'(?:中文|Chinese)[:：]\s*(.*?)(?:(?:日文|Japanese)[:：]|$)', sanitized, flags=re.DOTALL | re.IGNORECASE)
                if ch_fallback:
                    current_ch = ch_fallback.group(1).strip()
                else:
                    current_ch = sanitized.strip()

                if len(current_ch) > self.emitted_chinese_len:
                    new_chinese_delta = current_ch[self.emitted_chinese_len:]
                    self.chinese_extracted = current_ch
                    self.emitted_chinese_len = len(current_ch)

        # 2. Incremental Japanese Sentence Slicing
        ja_match = re.search(r'"japanese"\s*:\s*"((?:[^"\\]|\\.)*)', sanitized)
        if ja_match:
            raw_ja = ja_match.group(1)
            current_ja = self._unescape_json_string(raw_ja)
            self.japanese_extracted = current_ja

            all_sentences = split_japanese_sentences(current_ja)
            # If JSON object is not yet closed, the last sentence might still be growing
            if not sanitized.rstrip().endswith(('"}', '"}`', '"} \n`', '"} \n', '"}')):
                if all_sentences and not re.search(r'[。！？!?\n]$', all_sentences[-1]):
                    all_sentences = all_sentences[:-1]

            completed_text = "".join(all_sentences)
            if len(completed_text) > self.emitted_japanese_len:
                remaining = completed_text[self.emitted_japanese_len:]
                new_sentences = split_japanese_sentences(remaining)
                self.emitted_japanese_len = len(completed_text)
        elif self.is_plain_text_fallback:
            ja_fallback = re.search(r'(?:日文|Japanese)[:：]\s*(.*)$', sanitized, flags=re.DOTALL | re.IGNORECASE)
            if ja_fallback:
                current_ja = ja_fallback.group(1).strip()
                self.japanese_extracted = current_ja
                all_sentences = split_japanese_sentences(current_ja)
                if all_sentences and not re.search(r'[。！？!?\n]$', all_sentences[-1]):
                    all_sentences = all_sentences[:-1]
                completed_text = "".join(all_sentences)
                if len(completed_text) > self.emitted_japanese_len:
                    remaining = completed_text[self.emitted_japanese_len:]
                    new_sentences = split_japanese_sentences(remaining)
                    self.emitted_japanese_len = len(completed_text)

        return new_chinese_delta, new_sentences

    def finalize(self) -> Tuple[str, str, List[str]]:
        """
        Flushes parser buffer at end of stream.
        Returns:
            (full_chinese, full_japanese, remaining_unemitted_sentences)
        """
        sanitized = self.clean_markdown_delimiters(self.buffer).strip()

        # Try full JSON parsing
        try:
            parsed = json.loads(sanitized)
            if isinstance(parsed, dict):
                self.chinese_extracted = parsed.get("chinese", self.chinese_extracted)
                self.japanese_extracted = parsed.get("japanese", self.japanese_extracted)
        except Exception:
            # Try regex extraction for unclosed JSON
            ch_match = re.search(r'"chinese"\s*:\s*"((?:[^"\\]|\\.)*)', sanitized)
            if ch_match:
                self.chinese_extracted = self._unescape_json_string(ch_match.group(1))
            ja_match = re.search(r'"japanese"\s*:\s*"((?:[^"\\]|\\.)*)', sanitized)
            if ja_match:
                self.japanese_extracted = self._unescape_json_string(ja_match.group(1))

            # Fallback for structured text without valid JSON
            if not self.chinese_extracted and not self.japanese_extracted:
                ch_fallback = re.search(r'(?:中文|Chinese)[:：]\s*(.*?)(?:(?:日文|Japanese)[:：]|$)', sanitized, flags=re.DOTALL | re.IGNORECASE)
                ja_fallback = re.search(r'(?:日文|Japanese)[:：]\s*(.*)$', sanitized, flags=re.DOTALL | re.IGNORECASE)
                if ch_fallback:
                    self.chinese_extracted = ch_fallback.group(1).strip()
                if ja_fallback:
                    self.japanese_extracted = ja_fallback.group(1).strip()
                if not self.chinese_extracted:
                    self.chinese_extracted = sanitized
                if not self.japanese_extracted:
                    self.japanese_extracted = self.chinese_extracted

        remaining_sentences: List[str] = []
        if self.japanese_extracted:
            all_sentences = split_japanese_sentences(self.japanese_extracted)
            emitted_so_far = self.emitted_japanese_len
            full_ja_text = "".join(all_sentences)
            if len(full_ja_text) > emitted_so_far:
                rem_text = full_ja_text[emitted_so_far:]
                if rem_text.strip():
                    remaining_sentences = split_japanese_sentences(rem_text)

        return self.chinese_extracted, self.japanese_extracted, remaining_sentences


# ============================================================================
# Chat Service (End-to-End Coordination)
# ============================================================================

class ChatService:
    """
    Coordinates multi-turn dialogue, LLM adapter streaming, incremental bilingual
    parsing, and low-latency sentence-by-sentence TTS audio generation.
    """

    def __init__(
        self,
        tts_service: Optional[TtsService] = None,
        db_path: Optional[Union[str, Path]] = None,
    ):
        settings = get_settings()
        self.tts_service = tts_service or TtsService()
        self.db_path = str(db_path or settings.db_path)
        self.session_manager = SessionManager(db_path=self.db_path)

    async def _get_active_llm_adapter(self, conn: Optional[aiosqlite.Connection] = None, provider_id: Optional[str] = None) -> Tuple[BaseLLMAdapter, str]:
        """
        Loads the configured or requested LLM adapter and target chat model from DB.
        """
        if conn is not None:
            if provider_id:
                provider = await crud.get_provider_raw(conn, provider_id)
            else:
                provider = await crud.get_active_provider_raw(conn)

            if provider:
                adapter = get_llm_adapter(provider)
                chat_model = provider.chat_model or "gpt-4o-mini"
                return adapter, chat_model

            adapter = get_llm_adapter("openai")
            return adapter, "gpt-4o-mini"

        async with get_db(self.db_path) as local_conn:
            if provider_id:
                provider = await crud.get_provider_raw(local_conn, provider_id)
            else:
                provider = await crud.get_active_provider_raw(local_conn)

            if provider:
                adapter = get_llm_adapter(provider)
                chat_model = provider.chat_model or "gpt-4o-mini"
                return adapter, chat_model

            # Fallback to OpenAI default if no provider registered
            adapter = get_llm_adapter("openai")
            return adapter, "gpt-4o-mini"


    async def _prepare_messages(
        self,
        conn: aiosqlite.Connection,
        session_id: str,
        user_prompt: str,
        character_name: Optional[str] = None,
    ) -> List[ChatMessage]:
        """
        Constructs system prompt and conversation history messages for LLM using SessionManager.
        """
        active_profile = await crud.get_active_voice_profile(conn)
        system_prompt = (
            active_profile.system_prompt
            if active_profile and active_profile.system_prompt
            else self.session_manager.DEFAULT_SYSTEM_TEMPLATE
        )
        char_name = character_name or (active_profile.name if active_profile else "四季夏目")

        settings_raw = await crud.get_settings_raw(conn)
        max_history = settings_raw.max_history_messages if settings_raw else 10

        return await self.session_manager.build_chat_messages(
            session_id=session_id,
            user_prompt=user_prompt,
            character_name=char_name,
            custom_system_prompt=system_prompt,
            max_messages=max_history,
        )

    async def stream_chat(
        self,
        prompt: str,
        session_id: str = "default",
        character_name: Optional[str] = None,
        provider_id: Optional[str] = None,
        tts_options: Optional[Dict[str, Any]] = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Asynchronously streams bilingual SSE events:
          - text: incremental Chinese delta tokens
          - audio_chunk: synthesized sentence audio URL and index
          - done: final complete Chinese/Japanese text and full audio URL
          - error: error detail if an exception occurs
        """
        start_time = time.time()
        parser = StreamingBilingualParser()
        chunk_index = 0
        audio_chunks: List[Dict[str, Any]] = []

        if cancel_event and cancel_event.is_set():
            logger.info("Stream chat cancelled before starting for session %s", session_id)
            return

        try:
            async with get_db(self.db_path) as conn:
                # Ensure session exists and record user message
                await crud.get_or_create_session(conn, session_id)
                await crud.add_message(conn, MessageCreate(
                    session_id=session_id,
                    role="user",
                    content_chinese=prompt,
                    content_japanese="",
                    audio_url="",
                    latency_ms=0,
                ))

                adapter, model_name = await self._get_active_llm_adapter(conn=conn, provider_id=provider_id)
                messages = await self._prepare_messages(conn, session_id, prompt, character_name)


            tts_queue: asyncio.Queue = asyncio.Queue()
            event_out_queue: asyncio.Queue = asyncio.Queue()
            producer_done = asyncio.Event()

            # Background TTS Consumer Worker
            async def tts_worker():
                chunk_index = 0
                while True:
                    sentence = await tts_queue.get()
                    if sentence is None:
                        tts_queue.task_done()
                        break
                    if not sentence.strip():
                        tts_queue.task_done()
                        continue
                    if cancel_event and cancel_event.is_set():
                        tts_queue.task_done()
                        break

                    try:
                        audio_url, local_path, _ = await self.tts_service.synthesize_to_file(
                            sentence,
                            options=tts_options,
                            filename_prefix=f"chunk_{chunk_index}",
                        )
                        chunk_data = {
                            "index": chunk_index,
                            "audio_url": audio_url,
                            "sentence": sentence,
                            "local_path": str(local_path),
                        }
                        audio_chunks.append(chunk_data)
                        await event_out_queue.put({
                            "event": "audio_chunk",
                            "data": {
                                "index": chunk_index,
                                "audio_url": audio_url,
                                "sentence": sentence,
                            },
                        })
                        chunk_index += 1
                    except Exception as e:
                        logger.warning("Failed to synthesize audio chunk %d for sentence '%s': %s", chunk_index, sentence, e)
                    finally:
                        tts_queue.task_done()

            worker_task = asyncio.create_task(tts_worker())

            # LLM Stream Producer
            async def llm_producer():
                try:
                    stream_gen = adapter.stream_chat(messages, model=model_name)
                    async for token in stream_gen:
                        if cancel_event and cancel_event.is_set():
                            break
                        delta_ch, completed_sentences = parser.feed_chunk(token)
                        if delta_ch:
                            await event_out_queue.put({
                                "event": "text",
                                "data": {
                                    "delta_chinese": delta_ch,
                                    "full_chinese": parser.chinese_extracted,
                                }
                            })
                        for sentence in completed_sentences:
                            await tts_queue.put(sentence)

                    full_ch, full_ja, rem_sentences = parser.finalize()
                    if len(full_ch) > parser.emitted_chinese_len:
                        rem_ch = full_ch[parser.emitted_chinese_len:]
                        await event_out_queue.put({
                            "event": "text",
                            "data": {
                                "delta_chinese": rem_ch,
                                "full_chinese": full_ch,
                            }
                        })
                    for sentence in rem_sentences:
                        await tts_queue.put(sentence)
                except Exception as exc:
                    logger.error("LLM Producer error: %s", exc)
                    await event_out_queue.put({"event": "error", "data": {"error": str(exc)}})
                finally:
                    await tts_queue.put(None)
                    producer_done.set()

            producer_task = asyncio.create_task(llm_producer())

            while True:
                if producer_done.is_set() and worker_task.done() and event_out_queue.empty():
                    break

                try:
                    event = await asyncio.wait_for(event_out_queue.get(), timeout=0.05)
                    yield event
                    event_out_queue.task_done()
                    if event.get("event") == "error":
                        producer_task.cancel()
                        worker_task.cancel()
                        return
                except asyncio.TimeoutError:
                    if cancel_event and cancel_event.is_set():
                        break
                    continue

            if cancel_event and cancel_event.is_set():
                logger.info("Stream chat cancelled for session %s", session_id)
                producer_task.cancel()
                worker_task.cancel()
                return

            # Ensure both tasks are finished
            await producer_task
            await worker_task

            full_chinese, full_japanese, _ = parser.finalize()
            total_audio_url = ""
            if audio_chunks:
                if len(audio_chunks) == 1:
                    total_audio_url = audio_chunks[0]["audio_url"]
                else:
                    try:
                        import wave
                        import uuid
                        full_filename = f"full_{uuid.uuid4().hex[:12]}.wav"
                        full_path = self.tts_service.audio_dir / full_filename
                        
                        data = []
                        params = None
                        for c in audio_chunks:
                            local_p_str = c.get("local_path", "")
                            if local_p_str:
                                p = Path(local_p_str)
                                if p.exists():
                                    with wave.open(str(p), "rb") as w:
                                        if params is None:
                                            params = w.getparams()
                                        data.append(w.readframes(w.getnframes()))
                        
                        if data and params:
                            with wave.open(str(full_path), "wb") as w_out:
                                w_out.setparams(params)
                                for d in data:
                                    w_out.writeframes(d)
                            total_audio_url = f"/audio/{full_filename}"
                        else:
                            total_audio_url = audio_chunks[0]["audio_url"]
                    except Exception as cat_err:
                        logger.warning("Failed to concatenate audio chunks: %s", cat_err)
                        total_audio_url = audio_chunks[0]["audio_url"]

            total_latency = int((time.time() - start_time) * 1000)

            # Persist assistant message in DB
            async with get_db(self.db_path) as conn:
                await crud.add_message(conn, MessageCreate(
                    session_id=session_id,
                    role="assistant",
                    content_chinese=full_chinese,
                    content_japanese=full_japanese,
                    audio_url=total_audio_url,
                    latency_ms=total_latency,
                ))

            # Clean local_path from audio_chunks before emitting to frontend
            clean_chunks = [
                {"index": c.get("index", i), "audio_url": c.get("audio_url", ""), "sentence": c.get("sentence", "")}
                for i, c in enumerate(audio_chunks)
            ]

            # Emit final done event
            yield {
                "event": "done",
                "data": {
                    "chinese": full_chinese,
                    "japanese": full_japanese,
                    "audio_url": total_audio_url,
                    "total_audio_url": total_audio_url,
                    "chunks": clean_chunks,
                    "latency_ms": total_latency,
                }
            }

        except Exception as exc:
            logger.error("Error in stream_chat pipeline: %s", exc, exc_info=True)
            yield {
                "event": "error",
                "data": {"error": str(exc)}
            }

    async def chat_sync(
        self,
        prompt: str,
        session_id: str = "default",
        character_name: Optional[str] = None,
        provider_id: Optional[str] = None,
        tts_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Synchronous non-streaming bilingual completion and TTS synthesis.
        """
        start_time = time.time()
        async with get_db(self.db_path) as conn:
            await crud.get_or_create_session(conn, session_id)
            await crud.add_message(conn, MessageCreate(
                session_id=session_id,
                role="user",
                content_chinese=prompt,
                content_japanese="",
                audio_url="",
                latency_ms=0,
            ))

            adapter, model_name = await self._get_active_llm_adapter(conn=conn, provider_id=provider_id)
            messages = await self._prepare_messages(conn, session_id, prompt, character_name)


        llm_response = await adapter.chat(messages, model=model_name)
        raw_text = llm_response.content

        # Parse bilingual response
        parser = StreamingBilingualParser()
        parser.feed_chunk(raw_text)
        chinese, japanese, _ = parser.finalize()

        if not chinese:
            chinese = raw_text
        if not japanese:
            japanese = chinese

        # Synthesize full audio
        audio_url = ""
        if japanese.strip():
            try:
                audio_url, _, _ = await self.tts_service.synthesize_to_file(
                    japanese,
                    options=tts_options,
                    filename_prefix="voice",
                )
            except Exception as e:
                logger.warning("TTS synthesis in chat_sync failed: %s", e)

        latency_ms = int((time.time() - start_time) * 1000)

        # Save assistant message to DB
        async with get_db(self.db_path) as conn:
            await crud.add_message(conn, MessageCreate(
                session_id=session_id,
                role="assistant",
                content_chinese=chinese,
                content_japanese=japanese,
                audio_url=audio_url,
                latency_ms=latency_ms,
            ))

        return {
            "session_id": session_id,
            "chinese": chinese,
            "japanese": japanese,
            "audio_url": audio_url,
            "audioUrl": audio_url,
            "latency_ms": latency_ms,
        }


__all__ = [
    "StreamingBilingualParser",
    "ChatService",
    "split_japanese_sentences",
]
