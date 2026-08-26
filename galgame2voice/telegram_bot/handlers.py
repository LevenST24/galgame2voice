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
from typing import Any, Dict, List, Optional, Tuple

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import ContextTypes, CallbackQueryHandler
    HAS_TELEGRAM = True
except ImportError:
    HAS_TELEGRAM = False
    Update = Any
    InlineKeyboardButton = Any
    InlineKeyboardMarkup = Any
    class _ContextTypes:
        DEFAULT_TYPE = Any
    ContextTypes = _ContextTypes
    CallbackQueryHandler = Any

from galgame2voice.database.session import get_db
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate, SettingsUpdate
from galgame2voice.services.chat_service import ChatService, StreamingBilingualParser
from galgame2voice.services.tts_service import TtsService
from galgame2voice.adapters.registry import get_stt_adapter
from galgame2voice.utils.audio_converter import convert_ogg_to_wav, convert_wav_to_ogg

logger = logging.getLogger("galgame2voice.telegram_bot.handlers")


class TelegramBotHandlers:
    """
    Coordinates message handling, voice note processing, per-user background task tracking,
    and native Telegram Inline Keyboard Interactive Console.
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

    async def build_main_console(self, chat_id: int) -> Tuple[str, Any]:
        """Constructs the rich text and inline keyboard for the Telegram Interactive Console."""
        profile = None
        settings = None
        provider = None
        affection = None
        msg_count = 0

        try:
            async with get_db(self.db_path) as conn:
                profile = await crud.get_active_voice_profile(conn)
                settings = await crud.get_settings_raw(conn)
                provider = await crud.get_active_provider(conn, mask=True)
                profile_id = profile.id if profile else 1
                try:
                    affection = await crud.get_or_create_character_affection(
                        conn, user_id=str(chat_id), character_id=profile_id
                    )
                except Exception:
                    affection = None
                try:
                    msgs = await crud.get_session_messages(conn, f"tg_{chat_id}")
                    msg_count = len(msgs)
                except Exception:
                    msg_count = 0
        except Exception as exc:
            logger.debug("Database read failed in build_main_console: %s", exc)

        char_name = profile.name if profile else "四季夏目 (默认)"
        speed = getattr(settings, "speed_factor", 1.05) if settings else 1.05
        split_m = getattr(settings, "text_split_method", "cut5") if settings else "cut5"
        prov_name = f"{provider.name} ({provider.chat_model})" if provider else "未配置"
        aff_str = f"Lv.{affection.affection_level} {affection.level_name} ({affection.current_emotion})" if affection else "Lv.1 初识"

        text = (
            "🎮 【Galgame2Voice 交互控制台】\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🌸 当前角色: {char_name}\n"
            f"⚡ 语音语速: {speed}x | 切分: {split_m}\n"
            f"🤖 对话模型: {prov_name}\n"
            f"💖 好感状态: {aff_str}\n"
            f"💬 对话轮数: {msg_count} 轮\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💡 点击下方内联按钮直接在手机端快捷设置："
        )

        reply_markup = None
        if HAS_TELEGRAM and InlineKeyboardButton and InlineKeyboardMarkup:
            keyboard = [
                [
                    InlineKeyboardButton("🎭 切换角色音色", callback_data="menu_voice"),
                    InlineKeyboardButton("⚡ 调节语音语速", callback_data="menu_speed"),
                ],
                [
                    InlineKeyboardButton("🤖 切换大模型", callback_data="menu_model"),
                    InlineKeyboardButton("💖 好感度档案", callback_data="menu_affection"),
                ],
                [
                    InlineKeyboardButton("🗑️ 清空当前对话", callback_data="action_reset"),
                    InlineKeyboardButton("🔄 刷新控制台", callback_data="menu_refresh"),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

        return text, reply_markup

    async def build_voice_menu(self) -> Tuple[str, Any]:
        """Constructs sub-menu for switching voice profiles."""
        profiles = []
        active_id = None
        try:
            async with get_db(self.db_path) as conn:
                profiles = await crud.list_voice_profiles(conn)
                active = await crud.get_active_voice_profile(conn)
                active_id = active.id if active else None
        except Exception as exc:
            logger.error("Database read failed in build_voice_menu: %s", exc)

        text = "🎭 【选择角色音色】\n请点击下方按钮切换你想对话的角色："
        keyboard = []
        for p in profiles:
            is_cur = (active_id is not None and p.id == active_id)
            prefix = "🌸 " if is_cur else "▫️ "
            suffix = " (当前)" if is_cur else ""
            keyboard.append([InlineKeyboardButton(f"{prefix}{p.name}{suffix}", callback_data=f"set_voice_{p.id}")])

        keyboard.append([InlineKeyboardButton("🔙 返回主控制台", callback_data="menu_main")])
        reply_markup = InlineKeyboardMarkup(keyboard) if HAS_TELEGRAM and InlineKeyboardMarkup else None
        return text, reply_markup

    async def build_speed_menu(self) -> Tuple[str, Any]:
        """Constructs sub-menu for adjusting voice speed factor."""
        current_speed = 1.05
        try:
            async with get_db(self.db_path) as conn:
                settings = await crud.get_settings_raw(conn)
                current_speed = getattr(settings, "speed_factor", 1.05) if settings else 1.05
        except Exception as exc:
            logger.error("Database read failed in build_speed_menu: %s", exc)

        text = f"⚡ 【调节语音语速】\n当前语速: {current_speed}x\n请选择你期望的发音语速："
        speeds = [0.8, 0.9, 1.0, 1.05, 1.1, 1.2, 1.3, 1.5]
        row1 = []
        row2 = []
        for s in speeds[:4]:
            mark = "✓ " if abs(current_speed - s) < 0.01 else ""
            row1.append(InlineKeyboardButton(f"{mark}{s}x", callback_data=f"set_speed_{s}"))
        for s in speeds[4:]:
            mark = "✓ " if abs(current_speed - s) < 0.01 else ""
            row2.append(InlineKeyboardButton(f"{mark}{s}x", callback_data=f"set_speed_{s}"))

        keyboard = [row1, row2, [InlineKeyboardButton("🔙 返回主控制台", callback_data="menu_main")]]
        reply_markup = InlineKeyboardMarkup(keyboard) if HAS_TELEGRAM and InlineKeyboardMarkup else None
        return text, reply_markup

    async def build_model_menu(self) -> Tuple[str, Any]:
        """Constructs sub-menu for switching active LLM provider."""
        providers = []
        active_id = None
        try:
            async with get_db(self.db_path) as conn:
                providers = await crud.list_providers(conn)
                active = await crud.get_active_provider(conn)
                active_id = active.id if active else None
        except Exception as exc:
            logger.error("Database read failed in build_model_menu: %s", exc)

        text = "🤖 【切换大模型提供商】\n请选择活跃的 LLM 接口供应商："
        keyboard = []
        # Create 2-column buttons for neat layout
        temp_row = []
        for prov in providers:
            is_cur = (active_id is not None and prov.id == active_id)
            prefix = "🟢 " if is_cur else "⚪ "
            suffix = " (当前)" if is_cur else ""
            btn = InlineKeyboardButton(f"{prefix}{prov.name}{suffix}", callback_data=f"set_model_{prov.id}")
            if is_cur:
                # Active provider on its own prominent row
                if temp_row:
                    keyboard.append(temp_row)
                    temp_row = []
                keyboard.append([btn])
            else:
                temp_row.append(btn)
                if len(temp_row) == 2:
                    keyboard.append(temp_row)
                    temp_row = []
        if temp_row:
            keyboard.append(temp_row)

        keyboard.append([InlineKeyboardButton("🔙 返回主控制台", callback_data="menu_main")])
        reply_markup = InlineKeyboardMarkup(keyboard) if HAS_TELEGRAM and InlineKeyboardMarkup else None
        return text, reply_markup

    async def build_affection_menu(self, chat_id: int) -> Tuple[str, Any]:
        """Constructs sub-menu for displaying affection details and emotion."""
        profile = None
        affection = None
        msg_count = 0
        try:
            async with get_db(self.db_path) as conn:
                profile = await crud.get_active_voice_profile(conn)
                profile_id = profile.id if profile else 1
                try:
                    affection = await crud.get_or_create_character_affection(
                        conn, user_id=str(chat_id), character_id=profile_id
                    )
                except Exception:
                    affection = None
                try:
                    msgs = await crud.get_session_messages(conn, f"tg_{chat_id}")
                    msg_count = len(msgs)
                except Exception:
                    msg_count = 0
        except Exception as exc:
            logger.debug("Database read failed in build_affection_menu: %s", exc)

        char_name = profile.name if profile else "四季夏目"
        aff_level = affection.affection_level if affection else 1
        aff_name = affection.level_name if affection else "初识"
        aff_pts = affection.affection_score if affection else 0
        aff_emo = affection.current_emotion if affection else "平静"
        aff_nick = affection.custom_nickname if (affection and affection.custom_nickname) else "未设定"

        text = (
            f"💖 【{char_name} 的好感度档案】\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"• 好感等级: Lv.{aff_level}（{aff_name}）\n"
            f"• 好感点数: {aff_pts} pts\n"
            f"• 当前心境: {aff_emo}\n"
            f"• 你的昵称: {aff_nick}\n"
            f"• 累计对话: {msg_count} 轮\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💡 与角色多互动交流可以提升好感度并解锁更亲密的专属语音！"
        )
        keyboard = [
            [InlineKeyboardButton("🔄 刷新好感档案", callback_data="menu_affection")],
            [InlineKeyboardButton("🔙 返回主控制台", callback_data="menu_main")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard) if HAS_TELEGRAM and InlineKeyboardMarkup else None
        return text, reply_markup

    async def handle_callback_query(self, update: Any, context: Optional[Any] = None) -> None:
        """Handles inline button clicks in Telegram."""
        query = getattr(update, "callback_query", None)
        if not query:
            return
        data = getattr(query, "data", "") or ""
        chat_id = update.effective_chat.id if hasattr(update, "effective_chat") and update.effective_chat else 0

        try:
            if data in ("menu_main", "menu_refresh"):
                text, markup = await self.build_main_console(chat_id)
                if hasattr(query, "answer"):
                    await query.answer("已刷新控制台" if data == "menu_refresh" else None)
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data == "menu_voice":
                text, markup = await self.build_voice_menu()
                if hasattr(query, "answer"):
                    await query.answer()
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data.startswith("set_voice_"):
                profile_id = int(data.split("_")[-1])
                char_name = "目标角色"
                try:
                    async with get_db(self.db_path) as conn:
                        from galgame2voice.services.voice_manager import get_voice_manager
                        await get_voice_manager().switch_active_profile(profile_id)
                        profile = await crud.get_voice_profile(conn, profile_id)
                        if profile:
                            char_name = profile.name
                except Exception as exc:
                    logger.warning("Voice switch exception: %s", exc)
                if hasattr(query, "answer"):
                    await query.answer(f"🌸 音色已切换为: {char_name}", show_alert=True)
                text, markup = await self.build_main_console(chat_id)
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data == "menu_speed":
                text, markup = await self.build_speed_menu()
                if hasattr(query, "answer"):
                    await query.answer()
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data.startswith("set_speed_"):
                new_speed = float(data.split("_")[-1])
                try:
                    async with get_db(self.db_path) as conn:
                        await crud.update_settings(conn, SettingsUpdate(speed_factor=new_speed))
                except Exception as exc:
                    logger.warning("Speed update exception: %s", exc)
                if hasattr(query, "answer"):
                    await query.answer(f"⚡ 语速已调整为: {new_speed}x", show_alert=True)
                text, markup = await self.build_main_console(chat_id)
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data == "menu_model":
                text, markup = await self.build_model_menu()
                if hasattr(query, "answer"):
                    await query.answer()
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data.startswith("set_model_"):
                provider_id = data.replace("set_model_", "")
                prov_name = provider_id
                try:
                    async with get_db(self.db_path) as conn:
                        await crud.set_active_provider(conn, provider_id)
                        prov = await crud.get_provider(conn, provider_id)
                        if prov:
                            prov_name = prov.name
                except Exception as exc:
                    logger.warning("Model switch exception: %s", exc)
                if hasattr(query, "answer"):
                    await query.answer(f"🤖 已激活大模型: {prov_name}", show_alert=True)
                text, markup = await self.build_main_console(chat_id)
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data == "menu_affection":
                text, markup = await self.build_affection_menu(chat_id)
                if hasattr(query, "answer"):
                    await query.answer()
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data == "action_reset":
                session_id = f"tg_{chat_id}"
                self.cancel_user_task(chat_id)
                try:
                    async with get_db(self.db_path) as conn:
                        await crud.clear_session_messages(conn, session_id)
                except Exception as exc:
                    logger.warning("Could not clear session: %s", exc)
                if hasattr(query, "answer"):
                    await query.answer("🗑️ 当前会话记忆已清空！", show_alert=True)
                text, markup = await self.build_main_console(chat_id)
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

        except Exception as exc:
            logger.error("Error processing callback query '%s': %s", data, exc, exc_info=True)
            if hasattr(query, "answer"):
                try:
                    await query.answer(f"操作异常: {exc}", show_alert=True)
                except Exception:
                    pass

    async def handle_start(self, update: Any, context: Optional[Any] = None) -> str:
        """Handler for /start command."""
        reply = (
            "你好！我是你的二次元AI伴侣。\n"
            "随时发送文字或语音消息与我对话吧！\n\n"
            "🎮 支持的快捷指令：\n"
            "• /console - 打开原生交互控制台（音色/语速/模型快捷切换）\n"
            "• /voice - 查看当前音色与语音设置\n"
            "• /model - 查看模型与接口配置\n"
            "• /reset - 清空当前对话历史\n"
            "• /help - 查看完整帮助信息"
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

        profile_name = profile.name if profile else "四季夏目 (默认)"
        speed = getattr(settings, "speed_factor", 1.05) if settings else 1.05
        temp = getattr(settings, "temperature", 0.8) if settings else 0.8
        split_m = getattr(settings, "text_split_method", "cut5") if settings else "cut5"
        batch_s = getattr(settings, "batch_size", 1) if settings else 1

        reply = (
            f"【当前音色】\n"
            f"• 角色名称: {profile_name}\n"
            f"• 语速: {speed}x\n"
            f"• 温度: {temp}\n"
            f"• 切分方式: {split_m}\n"
            f"• 批量大小: {batch_s}\n\n"
            f"💡 发送 /console 可直接在手机端点击按钮切换音色与调节语速！"
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
                f"• 语音识别模型: {provider.stt_model or '(未配置)'}\n\n"
                f"💡 发送 /console 可直接在手机端点击按钮无缝切换大模型！"
            )
        else:
            reply = "未找到已配置的活跃提供商。"

        if hasattr(update, "message") and update.message:
            await update.message.reply_text(reply)
        elif hasattr(update, "effective_chat") and update.effective_chat and context and hasattr(context, "bot"):
            await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
        return reply

    async def handle_console(self, update: Any, context: Optional[Any] = None) -> str:
        """Handler for /console, /menu, /settings command rendering native Inline Keyboard Console."""
        chat_id = update.effective_chat.id if hasattr(update, "effective_chat") and update.effective_chat else 0
        text, markup = await self.build_main_console(chat_id)

        if hasattr(update, "message") and update.message:
            if markup:
                await update.message.reply_text(text, reply_markup=markup)
            else:
                await update.message.reply_text(text)
        elif hasattr(update, "effective_chat") and update.effective_chat and context and hasattr(context, "bot"):
            try:
                if markup:
                    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
                else:
                    await context.bot.send_message(chat_id=chat_id, text=text)
            except TypeError:
                await context.bot.send_message(chat_id=chat_id, text=text)
        return text

    async def handle_help(self, update: Any, context: Optional[Any] = None) -> str:
        """Handler for /help command."""
        reply = (
            "【支持的指令】\n"
            "/console - 打开原生交互控制台（音色/语速/模型切换）\n"
            "/voice - 查看当前音色与语音设置\n"
            "/model - 查看当前 LLM / STT 模型设置\n"
            "/reset - 清空当前对话上下文\n"
            "/help - 查看此帮助信息"
        )
        if hasattr(update, "message") and update.message:
            await update.message.reply_text(reply)
        elif hasattr(update, "effective_chat") and update.effective_chat and context and hasattr(context, "bot"):
            await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
        return reply

    async def handle_unknown(self, update: Any, context: Optional[Any] = None) -> str:
        """Handler for unknown commands."""
        reply = "未知指令，支持 /console, /voice, /model, /reset, /help"
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
                    if not wav_bytes:
                        logger.warning("TTS synthesis returned empty bytes for chat_id=%d", chat_id)
                        return

                    # Convert WAV to OGG/Opus for Telegram voice note
                    ogg_bytes = await convert_wav_to_ogg(wav_bytes)
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
