"""
Telegram Bot Command & Message Handlers for galgame2voice.
Implements:
- Immediate Chinese text reply + asynchronous background Japanese voice queue.
- Per-user task cancellation on new input (interruption handling).
- Multi-user isolation across concurrent chat IDs.
- Voice note download, OGG -> WAV conversion, STT transcription, and response dispatch.
- Slash command dispatch table (/start, /reset, /voice, /character|/char|/switch, /model,
  /nickname|/name, /console|/menu|/settings, /help), plus a catch-all unknown-command handler.
"""

import asyncio
from io import BytesIO
import logging
from typing import Any

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    HAS_TELEGRAM = True
except ImportError:
    HAS_TELEGRAM = False
    InlineKeyboardButton = Any
    InlineKeyboardMarkup = Any

from galgame2voice.database.session import get_db
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate, CharacterAffectionUpdate
from galgame2voice.services.chat_service import ChatService, StreamingBilingualParser
from galgame2voice.services.memory_service import MemoryService
from galgame2voice.services.tts_service import TtsService
from galgame2voice.adapters.registry import get_stt_adapter
from galgame2voice.utils.audio_converter import convert_ogg_to_wav, convert_wav_to_ogg
from galgame2voice.utils.japanese_phonetics import strip_stage_directions
from galgame2voice.utils.logger import sanitize_error_detail
from galgame2voice.telegram_bot.console_menus import (
    ADMIN_CALLBACK_ACTIONS,
    ADMIN_CALLBACK_PREFIXES,
    build_affection_menu,
    build_batch_menu,
    build_history_menu,
    build_interval_menu,
    build_main_console,
    build_metrics_menu,
    build_model_menu,
    build_sampling_menu,
    build_speed_menu,
    build_split_menu,
    build_temp_menu,
    build_tts_menu,
    build_voice_menu,
    handle_callback_query,
    resolve_effective_user_id,
    resolve_session_key,
)

logger = logging.getLogger("galgame2voice.telegram_bot.handlers")


class TelegramBotHandlers:
    """
    Coordinates message handling, voice note processing, per-user background task tracking,
    and native Telegram Inline Keyboard Interactive Console.
    """

    def __init__(
        self,
        chat_service: ChatService | None = None,
        tts_service: TtsService | None = None,
        db_path: str | None = None,
        admin_ids: list[int] | None = None,
    ):
        self.db_path = db_path
        self.chat_service = chat_service or ChatService(db_path=db_path)
        self.tts_service = tts_service or TtsService()
        # User voice synthesis background tasks mapped by chat_id
        self.user_tasks: dict[int, asyncio.Task] = {}
        # Telegram user IDs allowed to use the bot; empty set = fail-closed (nobody authorized)
        self.admin_ids: set = set(admin_ids or [])

    def _is_admin(self, update: Any) -> bool:
        # Fail-closed: an empty whitelist must NOT mean "everyone is admin".
        if not self.admin_ids:
            return False
        return resolve_effective_user_id(update) in self.admin_ids

    async def _check_admin_authorized(self, update: Any, context: Any, message_type: str = "message") -> bool:
        """Verifies if user is authorized. If not, logs warning, notifies user, and returns False."""
        if not self._is_admin(update):
            uid = resolve_effective_user_id(update)
            logger.warning("Rejected %s from unauthorized Telegram user_id=%d", message_type, uid)
            await self._safe_send_message(update, context, "抱歉，你没有使用本机器人的权限。")
            return False
        return True

    # Backward compatibility aliases for external callers / tests
    _effective_user_id = staticmethod(resolve_effective_user_id)
    _session_key = staticmethod(resolve_session_key)

    def cancel_user_task(self, chat_id: int) -> None:
        """Cancels active background voice task for given chat_id if running."""
        task = self.user_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()
            logger.info("Cancelled ongoing voice synthesis task for chat_id=%d", chat_id)

    async def build_main_console(self, chat_id: int, user_id: int = 0) -> tuple[str, Any]:
        """Constructs the rich text and inline keyboard for the Telegram Interactive Console."""
        return await build_main_console(chat_id, user_id, db_path=self.db_path, session_key_fn=resolve_session_key)

    async def build_voice_menu(self) -> tuple[str, Any]:
        """Constructs rich sub-menu for switching voice profiles with 2-column layout and active character card."""
        return await build_voice_menu(db_path=self.db_path)

    async def build_model_menu(self) -> tuple[str, Any]:
        """Constructs sub-menu for switching active LLM provider with API key safety indicators."""
        return await build_model_menu(db_path=self.db_path)

    async def build_tts_menu(self) -> tuple[str, Any]:
        """Constructs sub-menu for advanced TTS parameters."""
        return await build_tts_menu(db_path=self.db_path)

    async def build_speed_menu(self) -> tuple[str, Any]:
        """Constructs sub-menu for adjusting voice speed factor."""
        return await build_speed_menu(db_path=self.db_path)

    async def build_temp_menu(self) -> tuple[str, Any]:
        """Constructs sub-menu for adjusting voice temperature."""
        return await build_temp_menu(db_path=self.db_path)

    async def build_split_menu(self) -> tuple[str, Any]:
        """Constructs sub-menu for text split method."""
        return await build_split_menu(db_path=self.db_path)

    async def build_sampling_menu(self) -> tuple[str, Any]:
        """Constructs sub-menu for Top-K and Top-P sampling parameters."""
        return await build_sampling_menu(db_path=self.db_path)

    async def build_batch_menu(self) -> tuple[str, Any]:
        """Constructs sub-menu for batch size."""
        return await build_batch_menu(db_path=self.db_path)

    async def build_interval_menu(self) -> tuple[str, Any]:
        """Constructs sub-menu for fragment interval."""
        return await build_interval_menu(db_path=self.db_path)

    async def build_history_menu(self) -> tuple[str, Any]:
        """Constructs sub-menu for conversational memory history length."""
        return await build_history_menu(db_path=self.db_path)

    async def build_metrics_menu(self) -> tuple[str, Any]:
        """Constructs sub-menu for performance metrics and TTS cache control."""
        return await build_metrics_menu(db_path=self.db_path)

    async def build_affection_menu(self, chat_id: int, user_id: int = 0) -> tuple[str, Any]:
        """Constructs sub-menu for displaying affection details and emotion."""
        return await build_affection_menu(chat_id, user_id, db_path=self.db_path, session_key_fn=resolve_session_key)

    async def handle_callback_query(self, update: Any, context: Any | None = None) -> None:
        """Handles inline button clicks in Telegram."""
        await handle_callback_query(self, update, context)

    async def _safe_send_message(
        self, update: Any, context: Any | None, text: str, reply_markup: Any | None = None
    ) -> bool:
        """Safely sends Telegram message, absorbing network drops and client errors."""
        try:
            if hasattr(update, "message") and update.message:
                if reply_markup is not None:
                    await update.message.reply_text(text, reply_markup=reply_markup)
                else:
                    await update.message.reply_text(text)
                return True
            chat_id = update.effective_chat.id if hasattr(update, "effective_chat") and update.effective_chat else 0
            if chat_id and context and hasattr(context, "bot"):
                if reply_markup is not None:
                    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
                else:
                    await context.bot.send_message(chat_id=chat_id, text=text)
                return True
        except Exception as exc:
            logger.warning("Failed sending Telegram message: %s", exc)
        return False

    async def handle_start(self, update: Any, context: Any | None = None) -> str:
        """Handler for /start command."""
        reply = (
            "你好！我是你的二次元AI伴侣。\n"
            "随时发送文字或语音消息与我对话吧！\n\n"
            "🎮 支持的快捷指令：\n"
            "• /console | /menu | /settings - 打开原生交互控制台（音色/语速/模型快捷切换）\n"
            "• /character | /char | /switch - 切换角色与音色（支持 /character 栞那 快速切换）\n"
            "• /voice - 查看当前音色与语音设置\n"
            "• /model - 查看模型与接口配置\n"
            "• /nickname | /name <称呼> - 设置角色对你的专属称呼\n"
            "• /reset - 清空当前对话历史\n"
            "• /help - 查看完整帮助信息"
        )
        await self._safe_send_message(update, context, reply)
        return reply

    async def handle_nickname(self, update: Any, context: Any | None = None) -> str:
        """Handler for /nickname command to customize user nickname for character affection."""
        chat_id = update.effective_chat.id if hasattr(update, "effective_chat") and update.effective_chat else 0
        raw_text = ""
        if hasattr(update, "message") and update.message and update.message.text:
            raw_text = update.message.text.strip()

        parts = raw_text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            reply = (
                "🌸 【设置你的专属称呼】\n"
                "用法: `/nickname <你的称呼>`\n"
                "例如: `/nickname 昂晴` 或 `/nickname 欧尼酱`\n\n"
                "设置后，二次元伴侣会在对话中用这个名字称呼你哦！"
            )
        else:
            raw_nick = parts[1].strip()
            # Defensively sanitize nickname to prevent prompt injection and control character leakage
            new_nick = MemoryService.sanitize_fact_value(raw_nick, max_len=20)
            if not new_nick:
                reply = "⚠️ 称呼包含无效或特殊字符，请重新输入（支持中英文昵称，如「昂晴」「小夏目」）！"
            else:
                try:
                    profile_name = "夏目"
                    async with get_db(self.db_path) as conn:
                        profile = await crud.get_active_voice_profile(conn)
                        profile_id = profile.id if profile else 1
                        if profile and profile.name:
                            profile_name = profile.name.split("(")[0].strip()
                        # Ensure affection row exists
                        await crud.get_or_create_character_affection(
                            conn, user_id=str(chat_id), character_id=profile_id
                        )
                        await crud.update_character_affection(
                            conn,
                            user_id=str(chat_id),
                            character_id=profile_id,
                            updates=CharacterAffectionUpdate(custom_nickname=new_nick),
                        )
                    reply = f"🌸 称呼已成功更新为「{new_nick}」！\n{profile_name}在接下来的对话中就会这样称呼你啦~"
                except Exception as exc:
                    logger.error("Failed to update nickname for user %s: %s", chat_id, exc)
                    safe_err = sanitize_error_detail(exc)
                    reply = f"❌ 更新称呼失败: {safe_err}" if safe_err else "❌ 更新称呼失败，请稍后重试！"

        await self._safe_send_message(update, context, reply)
        return reply

    async def handle_reset(self, update: Any, context: Any | None = None) -> str:
        """Handler for /reset command."""
        chat_id = update.effective_chat.id if hasattr(update, "effective_chat") and update.effective_chat else 0
        session_id = resolve_session_key(chat_id, resolve_effective_user_id(update))
        self.cancel_user_task(chat_id)

        try:
            async with get_db(self.db_path) as conn:
                await crud.clear_session_messages(conn, session_id)
        except Exception as exc:
            logger.warning("Could not clear session %s via crud: %s; using SessionManager", session_id, exc)
            await self.chat_service.session_manager.clear_session(session_id)

        reply = "已清空当前对话上下文！"
        await self._safe_send_message(update, context, reply)
        return reply

    async def handle_voice(self, update: Any, context: Any | None = None) -> str:
        """Handler for /voice command."""
        profile = None
        settings = None
        try:
            async with get_db(self.db_path) as conn:
                profile = await crud.get_active_voice_profile(conn)
                settings = await crud.get_settings_raw(conn)
        except Exception as exc:
            logger.debug("Database read failed in handle_voice: %s", exc)

        profile_name = profile.name if profile else "未配置角色"
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
        reply_markup = None
        if HAS_TELEGRAM and InlineKeyboardButton and InlineKeyboardMarkup:
            reply_markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🎭 切换角色", callback_data="menu_voice"),
                    InlineKeyboardButton("🎙️ 语音调参", callback_data="menu_tts"),
                ],
                [
                    InlineKeyboardButton("🏠 打开控制台", callback_data="menu_main"),
                ],
            ])
        await self._safe_send_message(update, context, reply, reply_markup=reply_markup)
        return reply

    async def _lookup_character_profile_by_query(
        self,
        conn: Any,
        query_str: str,
        profiles: list[Any],
    ) -> Any | None:
        """Matches character profile by numeric ID, exact name, or CharacterManager flexible aliases."""
        if query_str.isdigit():
            matched = await crud.get_voice_profile(conn, int(query_str))
            if matched:
                return matched

        matched = await crud.get_voice_profile_by_name(conn, query_str)
        if matched:
            return matched

        try:
            from galgame2voice.services.character_manager import get_character_manager
            cm = get_character_manager()
            pkg = cm.get_character(query_str)
            candidate_names = []
            if pkg:
                candidate_names.extend([pkg.name, pkg.id])
            candidate_names.append(query_str)

            for p in profiles:
                p_low = p.name.lower()
                for c_name in candidate_names:
                    c_low = c_name.lower()
                    if c_low in p_low or p_low in c_low:
                        return p
        except Exception as cm_err:
            logger.debug("CharacterManager flexible lookup failed: %s", cm_err)

        return None

    async def _switch_character_weights(self, profile_id: int) -> tuple[str | None, str]:
        """Switches model weights in VoiceManager and updates active profile in SQLite.
        Returns (error_message, warning_note)."""
        warning_note = ""
        try:
            from galgame2voice.services.voice_manager import get_voice_manager, InsufficientMemoryError
            vm = get_voice_manager()
            try:
                switched = await vm.switch_active_profile(profile_id)
                if not switched:
                    warning_note = "\n⚠️ 提示：GPT-SoVITS 语音权重未能加载（引擎不可达，或引擎在线但拒绝了权重），已为您激活对话人设与好感档案。"
            except InsufficientMemoryError:
                raise
            except Exception as sw_err:
                logger.debug("VoiceManager weight switch skipped: %s", sw_err)
                warning_note = "\n⚠️ 提示：GPT-SoVITS 语音权重未能加载（引擎不可达，或引擎在线但拒绝了权重），已为您激活对话人设与好感档案。"

            async with get_db(self.db_path) as conn:
                await crud.set_active_voice_profile(conn, profile_id)
            return None, warning_note
        except InsufficientMemoryError as mem_err:
            return f"系统内存或显存不足，无法加载该角色模型: {mem_err}", ""
        except Exception as exc:
            return f"切换异常: {sanitize_error_detail(exc)}", ""

    async def handle_character(self, update: Any, context: Any | None = None) -> str:
        """
        Handler for /character, /char, /switch command.
        - /character: opens character selection inline keyboard menu.
        - /character <name|id>: directly switches to target character and replies with character profile card.
        """
        chat_id = update.effective_chat.id if hasattr(update, "effective_chat") and update.effective_chat else 0
        user_id = resolve_effective_user_id(update)
        raw_text = ""
        if hasattr(update, "message") and update.message and update.message.text:
            raw_text = update.message.text.strip()

        parts = raw_text.split(maxsplit=1)
        # If user just typed /character or /char with no argument -> render character selection menu
        if len(parts) < 2 or not parts[1].strip():
            text, markup = await self.build_voice_menu()
            await self._safe_send_message(update, context, text, reply_markup=markup)
            return text

        # User passed a character identifier/name to switch directly
        query_str = parts[1].strip()
        matched_profile = None
        profiles = []

        try:
            async with get_db(self.db_path) as conn:
                profiles = await crud.list_voice_profiles(conn)
                matched_profile = await self._lookup_character_profile_by_query(conn, query_str, profiles)
        except Exception as exc:
            logger.warning("Database read exception in handle_character for query '%s': %s", query_str, exc)

        if not matched_profile:
            reply = self._build_character_not_found_reply(query_str, profiles)
            _, markup = await self.build_voice_menu()
            await self._safe_send_message(update, context, reply, reply_markup=markup)
            return reply

        # Perform atomic switch
        char_name = matched_profile.name
        err_msg, warning_note = await self._switch_character_weights(matched_profile.id)

        if err_msg:
            reply = f"⚠️ 切换至角色【{char_name}】失败: {err_msg}"
            await self._safe_send_message(update, context, reply)
            return reply

        # Fetch affection info for feedback
        affection = None
        try:
            async with get_db(self.db_path) as conn:
                affection = await crud.get_or_create_character_affection(
                    conn, user_id=str(user_id or chat_id), character_id=matched_profile.id
                )
        except Exception:
            affection = None

        reply = self._format_character_switch_reply(matched_profile, affection, warning_note)
        reply_markup = None
        if HAS_TELEGRAM and InlineKeyboardButton and InlineKeyboardMarkup:
            reply_markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🎭 切换其他角色", callback_data="menu_voice"),
                    InlineKeyboardButton("🏠 打开控制台", callback_data="menu_main"),
                ]
            ])
        await self._safe_send_message(update, context, reply, reply_markup=reply_markup)
        return reply

    @staticmethod
    def _build_character_not_found_reply(query_str: str, profiles: list[Any]) -> str:
        """Formats the not-found notification message when direct character lookup fails."""
        avail_names = "、".join([p.name.split("(")[0].strip() for p in profiles[:6]])
        if len(profiles) > 6:
            avail_names += " 等"
        return (
            f"❌ 未找到与「{query_str}」匹配的角色！\n\n"
            f"💡 当前可用角色：{avail_names or '（暂无）'}\n"
            f"💡 发送 `/character` 可直接在下方点击按钮选择角色。"
        )

    @staticmethod
    def _format_character_switch_reply(
        profile: Any,
        affection: Any | None,
        warning_note: str,
    ) -> str:
        """Formats the confirmation card reply when switching active character."""
        char_name = profile.name
        aff_str = f"Lv.{affection.affection_level} {affection.level_name} ({affection.current_emotion})" if affection else "Lv.1 初识"
        clean_name = char_name.split("(")[0].strip()
        desc = profile.description or "暂无角色简介"
        if len(desc) > 80:
            desc = desc[:77] + "..."

        return (
            f"🌸 【角色切换成功】\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎭 当前角色: {char_name}\n"
            f"📖 人设简介: {desc}\n"
            f"🎙️ 语言设定: {profile.prompt_lang} / {profile.text_lang}\n"
            f"💖 专属好感: {aff_str}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💬 现在就可以直接发送文字或语音与 {clean_name} 对话啦！{warning_note}"
        )

    async def handle_model(self, update: Any, context: Any | None = None) -> str:
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

        await self._safe_send_message(update, context, reply)
        return reply

    async def handle_console(self, update: Any, context: Any | None = None) -> str:
        """Handler for /console, /menu, /settings command rendering native Inline Keyboard Console."""
        chat_id = update.effective_chat.id if hasattr(update, "effective_chat") and update.effective_chat else 0
        text, markup = await self.build_main_console(chat_id, resolve_effective_user_id(update))
        await self._safe_send_message(update, context, text, reply_markup=markup)
        return text

    async def handle_help(self, update: Any, context: Any | None = None) -> str:
        """Handler for /help command."""
        reply = (
            "【支持的快捷指令】\n"
            "• /start - 显示欢迎语与快捷指令列表\n"
            "• /console | /menu | /settings - 打开原生交互控制台（音色/语速/模型切换）\n"
            "• /character | /char | /switch - 切换角色与音色（支持 /character 栞那 快速切换）\n"
            "• /voice - 查看当前音色与语音设置\n"
            "• /model - 查看当前 LLM / STT 模型设置\n"
            "• /nickname | /name <称呼> - 设置角色对你的专属称呼\n"
            "• /reset - 清空当前对话上下文\n"
            "• /help - 查看此帮助信息"
        )
        await self._safe_send_message(update, context, reply)
        return reply

    async def handle_unknown(self, update: Any, context: Any | None = None) -> str:
        """Handler for unknown commands."""
        reply = (
            "未知指令，支持 /start, /console, /menu, /settings, /character, /char, /switch, "
            "/voice, /model, /nickname, /name, /reset, /help"
        )
        await self._safe_send_message(update, context, reply)
        return reply

    def _spawn_background_voice_task(
        self,
        chat_id: int,
        bot: Any,
        japanese: str,
        dynamic_tts: dict[str, Any] | None,
    ) -> asyncio.Task:
        """Schedules background voice synthesis and tracks task in user_tasks with auto-cleanup."""
        async def background_voice_worker() -> None:
            try:
                if bot and hasattr(bot, "send_chat_action"):
                    try:
                        await bot.send_chat_action(chat_id=chat_id, action="record_voice")
                    except Exception as action_err:
                        logger.debug("Failed sending record_voice chat action for chat_id=%d: %s", chat_id, action_err)

                clean_japanese = strip_stage_directions(japanese).strip() or japanese
                tts_opts = dict(dynamic_tts or {})
                wav_bytes = await self.tts_service.synthesize(clean_japanese, options=tts_opts)
                if not wav_bytes:
                    logger.warning("TTS synthesis returned empty bytes for chat_id=%d", chat_id)
                    return

                ogg_bytes = await convert_wav_to_ogg(wav_bytes)
                caption = clean_japanese[:1020]
                await bot.send_voice(chat_id=chat_id, voice=ogg_bytes, caption=caption)
            except asyncio.CancelledError:
                logger.info("Voice synthesis cancelled for chat_id=%d", chat_id)
                return
            except Exception as exc:
                logger.error("Voice synthesis failed for chat_id=%d: %s", chat_id, exc)

        task = asyncio.create_task(background_voice_worker())
        self.user_tasks[chat_id] = task

        def _cleanup_task(t: asyncio.Task, cid: int = chat_id) -> None:
            if self.user_tasks.get(cid) is t:
                self.user_tasks.pop(cid, None)

        task.add_done_callback(_cleanup_task)
        return task

    async def _setup_user_chat_turn(
        self,
        session_id: str,
        effective_user_id: int,
        text: str,
    ) -> tuple[Any, str, int, list[Any]]:
        """Prepares session, persists user message, extracts memory facts, and retrieves LLM adapter & messages."""
        async with get_db(self.db_path) as conn:
            await crud.get_or_create_session(conn, session_id, channel="telegram", user_id=str(effective_user_id))
            await crud.add_message(conn, MessageCreate(
                session_id=session_id,
                role="user",
                content_chinese=text,
                content_japanese="",
                audio_url="",
                latency_ms=0,
            ))
            # Extract and persist facts into long-term memory
            profile = await crud.get_active_voice_profile(conn)
            profile_id = profile.id if profile else 1
            try:
                if hasattr(self.chat_service, "memory_service"):
                    await self.chat_service.memory_service.process_user_message(
                        user_id=str(effective_user_id),
                        character_id=profile_id,
                        message_text=text,
                        conn=conn,
                    )
            except Exception as mem_err:
                logger.debug("Telegram non-critical memory extraction exception: %s", mem_err)

            adapter, model_name, _provider_id = await self.chat_service.get_active_llm_adapter(conn=conn)
            messages = await self.chat_service.prepare_messages(conn, session_id, text)
            return adapter, model_name, profile_id, messages

    async def _persist_assistant_chat_turn(
        self,
        session_id: str,
        effective_user_id: int,
        profile_id: int,
        user_text: str,
        display_chinese: str,
        japanese: str,
    ) -> None:
        """Persists assistant reply to DB and triggers affection progression."""
        async with get_db(self.db_path) as conn:
            await crud.add_message(conn, MessageCreate(
                session_id=session_id,
                role="assistant",
                content_chinese=display_chinese,
                content_japanese=japanese,
                audio_url="",
                latency_ms=0,
            ))
            try:
                if hasattr(self.chat_service, "affection_service"):
                    await self.chat_service.affection_service.handle_turn_affection(
                        user_id=str(effective_user_id),
                        character_id=profile_id,
                        user_text=user_text,
                        assistant_text=display_chinese,
                    )
            except Exception as aff_err:
                logger.debug("Telegram non-critical affection update exception: %s", aff_err)

    async def process_text_chat(self, chat_id: int, text: str, bot: Any, user_id: int = 0) -> asyncio.Task:
        """
        Executes immediate text reply, immediately persists assistant turn to DB,
        extracts long-term memory facts, updates character affection state,
        and schedules background voice generation.
        Returns the spawned background voice asyncio.Task.
        """
        effective_user_id = user_id or chat_id
        # 1. Cancel previous pending voice task for this user if active
        self.cancel_user_task(chat_id)

        session_id = resolve_session_key(chat_id, effective_user_id)

        try:
            # 2. Query ChatService / LLM Adapter for bilingual response
            adapter, model_name, profile_id, messages = await self._setup_user_chat_turn(
                session_id=session_id,
                effective_user_id=effective_user_id,
                text=text,
            )

            # Send typing chat action so Telegram shows "typing..." in status bar
            if bot and hasattr(bot, "send_chat_action"):
                try:
                    await bot.send_chat_action(chat_id=chat_id, action="typing")
                except Exception as action_err:
                    logger.debug("Failed sending typing chat action for chat_id=%d: %s", chat_id, action_err)

            llm_response = await adapter.chat(messages, model=model_name)
            raw_text = llm_response.content

            chinese, japanese, parser = StreamingBilingualParser.parse_full_text(raw_text)

            display_chinese = strip_stage_directions(chinese).strip() or chinese
            dynamic_tts = parser.get_dynamic_tts_options(sentence_text=japanese)

            # 3. Send text reply immediately
            await bot.send_message(chat_id=chat_id, text=display_chinese)

            # 4. Immediately persist assistant message to DB & process affection progression
            await self._persist_assistant_chat_turn(
                session_id=session_id,
                effective_user_id=effective_user_id,
                profile_id=profile_id,
                user_text=text,
                display_chinese=display_chinese,
                japanese=japanese,
            )

            # 5. Schedule background voice synthesis task
            return self._spawn_background_voice_task(chat_id, bot, japanese, dynamic_tts)

        except Exception as exc:
            logger.error("Error generating LLM reply for chat_id=%d: %s", chat_id, exc, exc_info=True)
            safe_err = sanitize_error_detail(exc)
            err_detail = f": {safe_err}" if safe_err else ""
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"抱歉，大模型生成回复失败{err_detail}\n请在管理控制台检查当前模型提供商配置或 API Key。"
                )
            except Exception as send_err:
                logger.error("Failed to send error notification to Telegram chat_id=%d: %s", chat_id, send_err)

            # Return a pending no-op task so callers still get a Task to track
            async def _noop() -> None: pass
            return asyncio.create_task(_noop())

    async def handle_text_message(self, update: Any, context: Any) -> asyncio.Task | None:
        """Handler for normal text messages."""
        if not hasattr(update, "message") or not update.message or not getattr(update.message, "text", None):
            return None
        if not await self._check_admin_authorized(update, context, "text message"):
            return None
        chat_id = update.effective_chat.id
        text = update.message.text.strip()
        if text.startswith("/"):
            return None
        return await self.process_text_chat(chat_id, text, context.bot, user_id=resolve_effective_user_id(update))

    @staticmethod
    async def _download_tg_file_bytes(tg_file: Any) -> bytes:
        """Downloads Telegram file bytes into memory via download_as_bytearray or download_to_memory."""
        if hasattr(tg_file, "download_as_bytearray"):
            return bytes(await tg_file.download_as_bytearray())
        if hasattr(tg_file, "download_to_memory"):
            buf = BytesIO()
            await tg_file.download_to_memory(buf)
            return buf.getvalue()
        return b""

    async def handle_voice_message(self, update: Any, context: Any) -> asyncio.Task | None:
        """
        Handler for Telegram voice notes:
        Downloads OGG, converts to 16kHz mono WAV, transcribes via STT, and triggers text chat.
        """
        if not hasattr(update, "message") or not update.message or not getattr(update.message, "voice", None):
            return None
        if not await self._check_admin_authorized(update, context, "voice message"):
            return None
        chat_id = update.effective_chat.id
        voice = update.message.voice

        if getattr(voice, "file_size", 0) and voice.file_size > 15 * 1024 * 1024:
            await context.bot.send_message(
                chat_id=chat_id,
                text="语音消息过大（超过15MB），请发送简短语音消息或使用文字交互。",
            )
            return None

        try:
            # 1. Download voice file bytes
            tg_file = await context.bot.get_file(voice.file_id)
            ogg_bytes = await self._download_tg_file_bytes(tg_file)

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
            return await self.process_text_chat(chat_id, transcribed_text.strip(), context.bot, user_id=resolve_effective_user_id(update))

        except ValueError as val_err:
            logger.warning("Corrupted or unreadable voice file for chat_id=%d: %s", chat_id, val_err)
            if hasattr(update, "message") and update.message:
                await update.message.reply_text(
                    f"抱歉，语音解析失败，请重试！（原因：{sanitize_error_detail(val_err)}）"
                )
            return None
        except Exception as exc:
            logger.error("Error processing voice message for chat_id=%d: %s", chat_id, exc, exc_info=True)
            if hasattr(update, "message") and update.message:
                await update.message.reply_text("抱歉，语音处理出错，请重试！")
            return None


__all__ = ["TelegramBotHandlers", "HAS_TELEGRAM", "ADMIN_CALLBACK_PREFIXES", "ADMIN_CALLBACK_ACTIONS"]
