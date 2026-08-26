"""
Telegram Bot Command & Message Handlers for galgame2voice.
Implements:
- Immediate Chinese text reply + asynchronous background Japanese voice queue.
- Per-user task cancellation on new input (interruption handling).
- Multi-user isolation across concurrent chat IDs.
- Voice note download, OGG -> WAV conversion, STT transcription, and response dispatch.
- Slash command dispatch table (/start, /reset, /voice, /help, /model, /console, /unknown).
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

try:
    from telegram import Update
    from telegram.ext import ContextTypes
    HAS_TELEGRAM = True
except ImportError:
    HAS_TELEGRAM = False
    Update = Any
    class _ContextTypes:
        DEFAULT_TYPE = Any
    ContextTypes = _ContextTypes

from galgame2voice.database.session import get_db
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.services.chat_service import ChatService, StreamingBilingualParser
from galgame2voice.services.tts_service import TtsService
from galgame2voice.adapters.registry import get_stt_adapter
from galgame2voice.utils.audio_converter import convert_ogg_to_wav, convert_wav_to_ogg

logger = logging.getLogger("galgame2voice.telegram_bot.handlers")


class TelegramBotHandlers:
    """
    Coordinates message handling, voice note processing, and per-user background task tracking.
    """

    def __init__(
        self,
        chat_service: Optional[ChatService] = None,
        tts_service: Optional[TtsService] = None,
        db_path: Optional[str] = None,
    ):
        self.db_path = db_path
        self.chat_service = chat_service or ChatService(db_path=db_path)
        self.tts_service = tts_service or TtsService()
        # User voice synthesis background tasks mapped by chat_id
        self.user_tasks: Dict[int, asyncio.Task] = {}

    def cancel_user_task(self, chat_id: int) -> None:
        """Cancels active background voice task for given chat_id if running."""
        task = self.user_tasks.get(chat_id)
        if task and not task.done():
            task.cancel()
            logger.info("Cancelled ongoing voice synthesis task for chat_id=%d", chat_id)

    async def handle_start(self, update: Any, context: Optional[Any] = None) -> str:
        """Handler for /start command."""
        reply = (
            "你好！我是你的二次元AI伴侣。\n"
            "随时发文字或语音与我对话吧！\n\n"
            "支持的指令：\n"
            "• /reset - 清空对话历史\n"
            "• /voice - 查看当前音色与语音设置\n"
            "• /model - 查看模型与接口配置\n"
            "• /console - 获取专属网页控制台链接\n"
            "• /help - 查看完整帮助"
        )
        if hasattr(update, "message") and update.message:
            await update.message.reply_text(reply)
        elif hasattr(update, "effective_chat") and update.effective_chat and context and hasattr(context, "bot"):
            await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
        return reply

    async def handle_reset(self, update: Any, context: Optional[Any] = None) -> str:
        """Handler for /reset command."""
        chat_id = update.effective_chat.id if hasattr(update, "effective_chat") and update.effective_chat else 0
        session_id = f"tg_{chat_id}"
        self.cancel_user_task(chat_id)

        try:
            async with get_db(self.db_path) as conn:
                await crud.clear_session_messages(conn, session_id)
        except Exception as exc:
            logger.warning("Could not clear session via crud: %s; using SessionManager", exc)
            await self.chat_service.session_manager.clear_session(session_id)

        reply = "已清空当前对话上下文！"
        if hasattr(update, "message") and update.message:
            await update.message.reply_text(reply)
        elif hasattr(update, "effective_chat") and update.effective_chat and context and hasattr(context, "bot"):
            await context.bot.send_message(chat_id=chat_id, text=reply)
        return reply

    async def handle_voice(self, update: Any, context: Optional[Any] = None) -> str:
        """Handler for /voice command."""
        profile = None
        settings = None
        try:
            async with get_db(self.db_path) as conn:
                profile = await crud.get_active_voice_profile(conn)
                settings = await crud.get_settings_raw(conn)
        except Exception as exc:
            logger.debug("Database read failed in handle_voice: %s", exc)

        profile_name = profile.name if profile else "Arona (默认)"
        speed = getattr(settings, "speed_factor", 1.0) if settings else 1.0
        temp = getattr(settings, "temperature", 0.8) if settings else 0.8
        split_m = getattr(settings, "text_split_method", "cut5") if settings else "cut5"
        batch_s = getattr(settings, "batch_size", 1) if settings else 1

        reply = (
            f"【当前音色】\n"
            f"• 角色名称: {profile_name}\n"
            f"• 语速: {speed}x\n"
            f"• 温度: {temp}\n"
            f"• 切分方式: {split_m}\n"
            f"• 批量大小: {batch_s}"
        )
        if hasattr(update, "message") and update.message:
            await update.message.reply_text(reply)
        elif hasattr(update, "effective_chat") and update.effective_chat and context and hasattr(context, "bot"):
            await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
        return reply

    async def handle_model(self, update: Any, context: Optional[Any] = None) -> str:
        """Handler for /model command."""
        provider = None
        try:
            async with get_db(self.db_path) as conn:
                provider = await crud.get_active_provider(conn, mask=True)
        except Exception as exc:
            logger.debug("Database read failed in handle_model: %s", exc)

        if provider:
            reply = (
                f"【模型设置】\n"
                f"• 当前提供商: {provider.name} ({provider.id})\n"
                f"• 接口地址: {provider.api_base_url}\n"
                f"• API Key: {provider.api_key}\n"
                f"• 对话模型: {provider.chat_model}\n"
                f"• 语音识别模型: {provider.stt_model or '(未配置)'}"
            )
        else:
            reply = "未找到已配置的活跃提供商。"

        if hasattr(update, "message") and update.message:
            await update.message.reply_text(reply)
        elif hasattr(update, "effective_chat") and update.effective_chat and context and hasattr(context, "bot"):
            await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
        return reply

    async def handle_console(self, update: Any, context: Optional[Any] = None) -> str:
        """Handler for /console command (Restricted to private chat and authorized admin)."""
        chat_type = getattr(getattr(update, "effective_chat", None), "type", "private")
        if chat_type != "private":
            reply = "安全限制：控制台链接包含管理员权限令牌，仅支持在私聊中获取！"
            if hasattr(update, "message") and update.message:
                await update.message.reply_text(reply)
            return reply

        settings = None
        try:
            async with get_db(self.db_path) as conn:
                settings = await crud.get_settings_raw(conn)
        except Exception as exc:
            logger.debug("Database read failed in handle_console: %s", exc)

        # Admin ID whitelist check if configured
        user_id = str(getattr(getattr(update, "effective_user", None), "id", ""))
        admin_ids_str = getattr(settings, "telegram_admin_ids", "") if settings else ""
        if admin_ids_str:
            admin_list = [a.strip() for a in admin_ids_str.split(",") if a.strip()]
            if admin_list and user_id not in admin_list:
                reply = "权限不足：当前 Telegram 用户未在管理员白名单中。"
                if hasattr(update, "message") and update.message:
                    await update.message.reply_text(reply)
                return reply

        base_url = settings.console_url.rstrip("/") if settings and settings.console_url else "http://localhost:8080"
        token = settings.console_token if settings else ""
        link = f"{base_url}/settings.html?token={token}" if token else f"{base_url}/settings.html"
        reply = f"你的专属控制台链接（请勿泄露给他人）：\n{link}"

        if hasattr(update, "message") and update.message:
            await update.message.reply_text(reply)
        elif hasattr(update, "effective_chat") and update.effective_chat and context and hasattr(context, "bot"):
            await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
        return reply

    async def handle_help(self, update: Any, context: Optional[Any] = None) -> str:
        """Handler for /help command."""
        reply = (
            "【支持的指令】\n"
            "/start - 启动并查看欢迎语\n"
            "/reset - 清空当前对话上下文\n"
            "/voice - 查看当前音色与语音设置\n"
            "/model - 查看当前 LLM / STT 模型设置\n"
            "/console - 获取专属网页控制台链接（仅限私聊）\n"
            "/help - 查看此帮助信息"
        )
        if hasattr(update, "message") and update.message:
            await update.message.reply_text(reply)
        elif hasattr(update, "effective_chat") and update.effective_chat and context and hasattr(context, "bot"):
            await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
        return reply

    async def handle_unknown(self, update: Any, context: Optional[Any] = None) -> str:
        """Handler for unknown commands."""
        reply = "未知指令，支持 /start, /reset, /voice, /help"
        if hasattr(update, "message") and update.message:
            await update.message.reply_text(reply)
        elif hasattr(update, "effective_chat") and update.effective_chat and context and hasattr(context, "bot"):
            await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
        return reply

    async def process_text_chat(self, chat_id: int, text: str, bot: Any) -> asyncio.Task:
        """
        Executes immediate text reply, immediately persists assistant turn to DB,
        and schedules background voice generation.
        Returns the spawned background voice asyncio.Task.
        """
        # 1. Cancel previous pending voice task for this user if active
        self.cancel_user_task(chat_id)

        session_id = f"tg_{chat_id}"

        # 2. Query ChatService / LLM Adapter for bilingual response
        async with get_db(self.db_path) as conn:
            await crud.get_or_create_session(conn, session_id, channel="telegram", user_id=str(chat_id))
            await crud.add_message(conn, MessageCreate(
                session_id=session_id,
                role="user",
                content_chinese=text,
                content_japanese="",
                audio_url="",
                latency_ms=0,
            ))
            adapter, model_name, _provider_id = await self.chat_service.get_active_llm_adapter(conn=conn)
            messages = await self.chat_service.prepare_messages(conn, session_id, text)

        try:
            llm_response = await adapter.chat(messages, model=model_name)
            raw_text = llm_response.content

            parser = StreamingBilingualParser()
            parser.feed_chunk(raw_text)
            chinese, japanese, _ = parser.finalize()

            if not chinese:
                chinese = raw_text
            if not japanese:
                japanese = chinese

            # 3. Send text reply immediately
            await bot.send_message(chat_id=chat_id, text=chinese)

            # 4. Immediately persist assistant message to DB to maintain dialogue history
            async with get_db(self.db_path) as conn:
                await crud.add_message(conn, MessageCreate(
                    session_id=session_id,
                    role="assistant",
                    content_chinese=chinese,
                    content_japanese=japanese,
                    audio_url="",
                    latency_ms=0,
                ))

            # 5. Schedule background voice synthesis task
            async def background_voice_worker():
                try:
                    # Synthesize Japanese text to WAV bytes
                    wav_bytes = await self.tts_service.synthesize(japanese)
                    # Convert WAV to OGG/Opus for Telegram voice note
                    ogg_bytes = await convert_wav_to_ogg(wav_bytes)
                    # Send voice note
                    await bot.send_voice(chat_id=chat_id, voice=ogg_bytes, caption=japanese)
                except asyncio.CancelledError:
                    logger.info("Voice synthesis cancelled for chat_id=%d", chat_id)
                    return
                except Exception as exc:
                    logger.error("Voice synthesis failed for chat_id=%d: %s", chat_id, exc)

            task = asyncio.create_task(background_voice_worker())
            self.user_tasks[chat_id] = task
            return task

        except Exception as exc:
            logger.error("Error generating LLM reply for chat_id=%d: %s", chat_id, exc, exc_info=True)
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"抱歉，大模型生成回复失败: {exc}\n请在管理控制台检查当前模型提供商配置或 API Key。"
                )
            except Exception as send_err:
                logger.error("Failed to send error notification to Telegram chat_id=%d: %s", chat_id, send_err)
            
            # Return a resolved task
            async def _noop(): pass
            return asyncio.create_task(_noop())

    async def handle_text_message(self, update: Any, context: Any) -> Optional[asyncio.Task]:
        """Handler for normal text messages."""
        if not hasattr(update, "message") or not update.message or not getattr(update.message, "text", None):
            return None
        chat_id = update.effective_chat.id
        text = update.message.text.strip()
        if text.startswith("/"):
            return None
        return await self.process_text_chat(chat_id, text, context.bot)

    async def handle_voice_message(self, update: Any, context: Any) -> Optional[asyncio.Task]:
        """
        Handler for Telegram voice notes:
        Downloads OGG, converts to 16kHz mono WAV, transcribes via STT, and triggers text chat.
        """
        if not hasattr(update, "message") or not update.message or not getattr(update.message, "voice", None):
            return None
        chat_id = update.effective_chat.id
        voice = update.message.voice

        try:
            # 1. Download voice file bytes
            tg_file = await context.bot.get_file(voice.file_id)
            if hasattr(tg_file, "download_as_bytearray"):
                ogg_bytes = await tg_file.download_as_bytearray()
            elif hasattr(tg_file, "download_to_memory"):
                from io import BytesIO
                buf = BytesIO()
                await tg_file.download_to_memory(buf)
                ogg_bytes = buf.getvalue()
            else:
                ogg_bytes = b""

            # 2. Convert OGG to 16kHz mono WAV
            wav_bytes = await convert_ogg_to_wav(bytes(ogg_bytes))

            # 3. Transcribe via active STT adapter
            async with get_db(self.db_path) as conn:
                active_prov = await crud.get_active_provider_raw(conn)
                stt_adapter = get_stt_adapter(active_prov) if active_prov else get_stt_adapter("openai")

            transcribed_text = await stt_adapter.transcribe(wav_bytes, filename="voice.wav")
            if not transcribed_text or not transcribed_text.strip():
                await update.message.reply_text("抱歉，未能从语音中识别出有效内容。")
                return None

            # 4. Forward to text chat pipeline
            return await self.process_text_chat(chat_id, transcribed_text.strip(), context.bot)

        except ValueError as val_err:
            logger.warning("Corrupted or unreadable voice file for chat_id=%d: %s", chat_id, val_err)
            if hasattr(update, "message") and update.message:
                await update.message.reply_text("抱歉，语音解析失败，请重试！")
            return None
        except Exception as exc:
            logger.error("Error processing voice message for chat_id=%d: %s", chat_id, exc, exc_info=True)
            if hasattr(update, "message") and update.message:
                await update.message.reply_text("抱歉，语音处理出错，请重试！")
            return None


__all__ = ["TelegramBotHandlers", "HAS_TELEGRAM"]
