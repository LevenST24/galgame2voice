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
from galgame2voice.database.models import MessageCreate, SettingsUpdate, CharacterAffectionUpdate
from galgame2voice.services.chat_service import ChatService, StreamingBilingualParser
from galgame2voice.services.tts_service import TtsService
from galgame2voice.adapters.registry import get_stt_adapter
from galgame2voice.utils.audio_converter import convert_ogg_to_wav, convert_wav_to_ogg

logger = logging.getLogger("galgame2voice.telegram_bot.handlers")

# Callback data that mutates global state and therefore requires admin privileges
# when an admin whitelist is configured. Empty whitelist = open access (single-user setups).
ADMIN_CALLBACK_PREFIXES = (
    "set_voice_", "set_speed_", "set_temp_", "set_split_", "set_topk_",
    "set_topp_", "set_batch_", "set_interval_", "set_history_", "set_model_",
)
ADMIN_CALLBACK_ACTIONS = {"action_clear_cache"}


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
        admin_ids: Optional[List[int]] = None,
    ):
        self.db_path = db_path
        self.chat_service = chat_service or ChatService(db_path=db_path)
        self.tts_service = tts_service or TtsService()
        # User voice synthesis background tasks mapped by chat_id
        self.user_tasks: Dict[int, asyncio.Task] = {}
        # Telegram user IDs allowed to run admin commands; empty set = unrestricted
        self.admin_ids: set = set(admin_ids or [])

    def _effective_user_id(self, update: Any) -> int:
        """Resolves the individual Telegram user behind an update (0 if unknown)."""
        user = getattr(update, "effective_user", None)
        uid = getattr(user, "id", None) if user else None
        return int(uid) if uid else 0

    def _is_admin(self, update: Any) -> bool:
        if not self.admin_ids:
            return True
        return self._effective_user_id(update) in self.admin_ids

    def _session_key(self, chat_id: int, user_id: int) -> str:
        # Private chats (user_id == chat_id or unknown) keep the legacy
        # per-chat key so existing history survives; group chats append the
        # member's user id so each member gets private history/memory state.
        if not user_id or user_id == chat_id:
            return f"tg_{chat_id}"
        return f"tg_{chat_id}_{user_id}"

    def cancel_user_task(self, chat_id: int) -> None:
        """Cancels active background voice task for given chat_id if running."""
        task = self.user_tasks.get(chat_id)
        if task and not task.done():
            task.cancel()
            logger.info("Cancelled ongoing voice synthesis task for chat_id=%d", chat_id)

    async def build_main_console(self, chat_id: int, user_id: int = 0) -> Tuple[str, Any]:
        """Constructs the rich text and inline keyboard for the Telegram Interactive Console."""
        profile = None
        settings = None
        provider = None
        affection = None
        msg_count = 0
        cache_stats = {"total_files": 0, "total_size_mb": 0.0}

        try:
            async with get_db(self.db_path) as conn:
                profile = await crud.get_active_voice_profile(conn)
                settings = await crud.get_settings_raw(conn)
                provider = await crud.get_active_provider(conn, mask=True)
                profile_id = profile.id if profile else 1
                try:
                    affection = await crud.get_or_create_character_affection(
                        conn, user_id=str(user_id or chat_id), character_id=profile_id
                    )
                except Exception:
                    affection = None
                try:
                    msg_count = await crud.count_session_messages(conn, self._session_key(chat_id, user_id))
                except Exception:
                    msg_count = 0
                try:
                    cache_stats = await crud.get_tts_cache_stats(conn)
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("Database read failed in build_main_console: %s", exc)

        char_name = profile.name if profile else "四季夏目 (默认)"
        speed = getattr(settings, "speed_factor", 1.05) if settings else 1.05
        temp = getattr(settings, "temperature", 0.8) if settings else 0.8
        split_m = getattr(settings, "text_split_method", "cut5") if settings else "cut5"
        hist = getattr(settings, "max_history_messages", 10) if settings else 10
        prov_name = f"{provider.name} ({provider.chat_model})" if provider else "未配置"
        aff_str = f"Lv.{affection.affection_level} {affection.level_name} ({affection.current_emotion})" if affection else "Lv.1 初识"
        cache_str = f"{cache_stats.get('total_files', 0)}条 ({cache_stats.get('total_size_mb', 0.0)}MB)"

        text = (
            "🎮 【Galgame2Voice 全能交互控制台】\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🌸 角色音色: {char_name}\n"
            f"🤖 对话模型: {prov_name}\n"
            f"⚡ 语音参数: 语速 {speed}x | 温度 {temp} | 切分 {split_m}\n"
            f"🧠 对话记忆: 记忆 {hist} 轮 | 当前累计 {msg_count} 轮\n"
            f"💖 角色好感: {aff_str}\n"
            f"📊 语音缓存: {cache_str}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💡 请点击下方按钮进行手机端快捷调节："
        )

        reply_markup = None
        if HAS_TELEGRAM and InlineKeyboardButton and InlineKeyboardMarkup:
            keyboard = [
                [
                    InlineKeyboardButton("🎭 角色音色切换", callback_data="menu_voice"),
                    InlineKeyboardButton("🤖 切换大模型", callback_data="menu_model"),
                ],
                [
                    InlineKeyboardButton("🎙️ 语音合成调参", callback_data="menu_tts"),
                    InlineKeyboardButton("🧠 对话记忆轮数", callback_data="menu_history"),
                ],
                [
                    InlineKeyboardButton("💖 好感互动档案", callback_data="menu_affection"),
                    InlineKeyboardButton("📊 性能与缓存", callback_data="menu_metrics"),
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

    async def build_model_menu(self) -> Tuple[str, Any]:
        """Constructs sub-menu for switching active LLM provider with API key safety indicators."""
        providers = []
        active_id = None
        try:
            async with get_db(self.db_path) as conn:
                providers = await crud.list_providers(conn, mask=False)
                active = await crud.get_active_provider(conn)
                active_id = active.id if active else None
        except Exception as exc:
            logger.error("Database read failed in build_model_menu: %s", exc)

        text = (
            "🤖 【切换大模型提供商】\n"
            "请选择活跃的 LLM 接口供应商（已进行 API Key 状态校验）："
        )
        keyboard = []
        temp_row = []
        for prov in providers:
            is_cur = (active_id is not None and prov.id == active_id)
            has_key = bool(prov.api_key and prov.api_key.strip())

            if is_cur:
                prefix = "🟢 "
                suffix = " (当前)"
            elif has_key or prov.id == "custom":
                prefix = "⚪ "
                suffix = " ✓"
            else:
                prefix = "⚠️ "
                suffix = " (未配Key)"

            btn = InlineKeyboardButton(f"{prefix}{prov.name}{suffix}", callback_data=f"set_model_{prov.id}")
            if is_cur:
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

    async def build_tts_menu(self) -> Tuple[str, Any]:
        """Constructs sub-menu for advanced TTS parameters."""
        settings = None
        try:
            async with get_db(self.db_path) as conn:
                settings = await crud.get_settings_raw(conn)
        except Exception as exc:
            logger.error("Database read failed in build_tts_menu: %s", exc)

        speed = getattr(settings, "speed_factor", 1.05) if settings else 1.05
        temp = getattr(settings, "temperature", 0.8) if settings else 0.8
        split = getattr(settings, "text_split_method", "cut5") if settings else "cut5"
        top_k = getattr(settings, "top_k", 15) if settings else 15
        top_p = getattr(settings, "top_p", 1.0) if settings else 1.0
        batch = getattr(settings, "batch_size", 1) if settings else 1
        interval = getattr(settings, "fragment_interval", 0.3) if settings else 0.3

        text = (
            "🎙️ 【语音合成高级调参】\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"• 语速因子 (Speed): {speed}x\n"
            f"• 发音温度 (Temp): {temp}\n"
            f"• 切分方式 (Split): {split}\n"
            f"• 采样参数: Top-K={top_k} | Top-P={top_p}\n"
            f"• 批量生成 (Batch): {batch} 句\n"
            f"• 分句连播间隔: {interval}s\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💡 请选择要调节的语音参数项目："
        )

        keyboard = [
            [
                InlineKeyboardButton(f"⚡ 调节语速 ({speed}x)", callback_data="menu_speed"),
                InlineKeyboardButton(f"🌡️ 发音温度 ({temp})", callback_data="menu_temp"),
            ],
            [
                InlineKeyboardButton(f"✂️ 切分方式 ({split})", callback_data="menu_split"),
                InlineKeyboardButton(f"🎯 采样 (K={top_k}/P={top_p})", callback_data="menu_sampling"),
            ],
            [
                InlineKeyboardButton(f"📦 批量大小 ({batch})", callback_data="menu_batch"),
                InlineKeyboardButton(f"⏱️ 分句间隔 ({interval}s)", callback_data="menu_interval"),
            ],
            [
                InlineKeyboardButton("🔙 返回主控制台", callback_data="menu_main"),
            ],
        ]
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

        keyboard = [
            row1,
            row2,
            [
                InlineKeyboardButton("🔙 返回语音调参", callback_data="menu_tts"),
                InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard) if HAS_TELEGRAM and InlineKeyboardMarkup else None
        return text, reply_markup

    async def build_temp_menu(self) -> Tuple[str, Any]:
        """Constructs sub-menu for adjusting voice temperature."""
        current_temp = 0.8
        try:
            async with get_db(self.db_path) as conn:
                settings = await crud.get_settings_raw(conn)
                current_temp = getattr(settings, "temperature", 0.8) if settings else 0.8
        except Exception as exc:
            logger.error("Database read failed in build_temp_menu: %s", exc)

        text = (
            f"🌡️ 【调节发音温度 (Temperature)】\n"
            f"当前温度: {current_temp}\n"
            "温度越高声音越富有情感起伏与变化，越低则越平稳严谨："
        )
        temps = [
            (0.3, "0.3 (稳定沉着)"),
            (0.6, "0.6 (平稳自然)"),
            (0.8, "0.8 (标准推荐)"),
            (1.0, "1.0 (生动活泼)"),
            (1.2, "1.2 (高昂起伏)"),
        ]
        keyboard = []
        for t_val, t_label in temps:
            mark = "✓ " if abs(current_temp - t_val) < 0.01 else ""
            keyboard.append([InlineKeyboardButton(f"{mark}{t_label}", callback_data=f"set_temp_{t_val}")])

        keyboard.append([
            InlineKeyboardButton("🔙 返回语音调参", callback_data="menu_tts"),
            InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main"),
        ])
        reply_markup = InlineKeyboardMarkup(keyboard) if HAS_TELEGRAM and InlineKeyboardMarkup else None
        return text, reply_markup

    async def build_split_menu(self) -> Tuple[str, Any]:
        """Constructs sub-menu for text split method."""
        current_split = "cut5"
        try:
            async with get_db(self.db_path) as conn:
                settings = await crud.get_settings_raw(conn)
                current_split = getattr(settings, "text_split_method", "cut5") if settings else "cut5"
        except Exception as exc:
            logger.error("Database read failed in build_split_menu: %s", exc)

        text = (
            f"✂️ 【选择文本切分方式 (Text Split)】\n"
            f"当前切分方式: {current_split}\n"
            "选择语音合成长句时的自动切分断句策略："
        )
        splits = [
            ("cut5", "🌸 cut5 智能自然切分 (推荐)"),
            ("cut1", "✂️ cut1 凑四句切分"),
            ("cut2", "。 cut2 按句号切分"),
            ("cut3", "， cut3 按全标点切分"),
            ("cut4", "↵ cut4 按换行切分"),
            ("cut0", "🚫 cut0 不切分 (整段合成)"),
        ]
        keyboard = []
        for s_key, s_label in splits:
            mark = "✓ " if current_split == s_key else ""
            keyboard.append([InlineKeyboardButton(f"{mark}{s_label}", callback_data=f"set_split_{s_key}")])

        keyboard.append([
            InlineKeyboardButton("🔙 返回语音调参", callback_data="menu_tts"),
            InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main"),
        ])
        reply_markup = InlineKeyboardMarkup(keyboard) if HAS_TELEGRAM and InlineKeyboardMarkup else None
        return text, reply_markup

    async def build_sampling_menu(self) -> Tuple[str, Any]:
        """Constructs sub-menu for Top-K and Top-P sampling parameters."""
        top_k = 15
        top_p = 1.0
        try:
            async with get_db(self.db_path) as conn:
                settings = await crud.get_settings_raw(conn)
                if settings:
                    top_k = getattr(settings, "top_k", 15)
                    top_p = getattr(settings, "top_p", 1.0)
        except Exception as exc:
            logger.error("Database read failed in build_sampling_menu: %s", exc)

        text = (
            f"🎯 【调节 Top-K / Top-P 采样】\n"
            f"当前配置: Top-K = {top_k} | Top-P = {top_p}\n\n"
            "• Top-K 限制候选词采样范围（推荐 15）\n"
            "• Top-P 累积概率阈值（推荐 1.0）\n"
            "点击下方按钮进行微调："
        )

        row_k = []
        for k in [5, 10, 15, 20, 30]:
            mark = "✓" if top_k == k else ""
            row_k.append(InlineKeyboardButton(f"{mark}K={k}", callback_data=f"set_topk_{k}"))

        row_p = []
        for p in [0.6, 0.8, 0.9, 1.0]:
            mark = "✓" if abs(top_p - p) < 0.01 else ""
            row_p.append(InlineKeyboardButton(f"{mark}P={p}", callback_data=f"set_topp_{p}"))

        keyboard = [
            row_k,
            row_p,
            [
                InlineKeyboardButton("🔙 返回语音调参", callback_data="menu_tts"),
                InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard) if HAS_TELEGRAM and InlineKeyboardMarkup else None
        return text, reply_markup

    async def build_batch_menu(self) -> Tuple[str, Any]:
        """Constructs sub-menu for batch size."""
        current_batch = 1
        try:
            async with get_db(self.db_path) as conn:
                settings = await crud.get_settings_raw(conn)
                current_batch = getattr(settings, "batch_size", 1) if settings else 1
        except Exception as exc:
            logger.error("Database read failed in build_batch_menu: %s", exc)

        text = (
            f"📦 【调节批量生成大小 (Batch Size)】\n"
            f"当前大小: {current_batch} 句\n"
            "每次送入 GPU 推理的分句数量（增大可加快多分句合成，但增加显存）："
        )
        batches = [
            (1, "📦 1 (单句推理 / 最省显存)"),
            (2, "📦 2 (双句并行 / 均衡推荐)"),
            (4, "📦 4 (四句并发 / 极速模式)"),
        ]
        keyboard = []
        for b_val, b_label in batches:
            mark = "✓ " if current_batch == b_val else ""
            keyboard.append([InlineKeyboardButton(f"{mark}{b_label}", callback_data=f"set_batch_{b_val}")])

        keyboard.append([
            InlineKeyboardButton("🔙 返回语音调参", callback_data="menu_tts"),
            InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main"),
        ])
        reply_markup = InlineKeyboardMarkup(keyboard) if HAS_TELEGRAM and InlineKeyboardMarkup else None
        return text, reply_markup

    async def build_interval_menu(self) -> Tuple[str, Any]:
        """Constructs sub-menu for fragment interval."""
        current_interval = 0.3
        try:
            async with get_db(self.db_path) as conn:
                settings = await crud.get_settings_raw(conn)
                current_interval = getattr(settings, "fragment_interval", 0.3) if settings else 0.3
        except Exception as exc:
            logger.error("Database read failed in build_interval_menu: %s", exc)

        text = (
            f"⏱️ 【调节分句连播间隔 (Fragment Interval)】\n"
            f"当前间隔: {current_interval}s\n"
            "多分句语音连续播放时的停顿呼吸间隔："
        )
        intervals = [
            (0.1, "0.1s (紧凑急促)"),
            (0.2, "0.2s (轻快自然)"),
            (0.3, "0.3s (标准推荐)"),
            (0.5, "0.5s (舒缓沉浸)"),
        ]
        keyboard = []
        for i_val, i_label in intervals:
            mark = "✓ " if abs(current_interval - i_val) < 0.01 else ""
            keyboard.append([InlineKeyboardButton(f"{mark}{i_label}", callback_data=f"set_interval_{i_val}")])

        keyboard.append([
            InlineKeyboardButton("🔙 返回语音调参", callback_data="menu_tts"),
            InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main"),
        ])
        reply_markup = InlineKeyboardMarkup(keyboard) if HAS_TELEGRAM and InlineKeyboardMarkup else None
        return text, reply_markup

    async def build_history_menu(self) -> Tuple[str, Any]:
        """Constructs sub-menu for conversational memory history length."""
        current_hist = 10
        try:
            async with get_db(self.db_path) as conn:
                settings = await crud.get_settings_raw(conn)
                current_hist = getattr(settings, "max_history_messages", 10) if settings else 10
        except Exception as exc:
            logger.error("Database read failed in build_history_menu: %s", exc)

        text = (
            f"🧠 【调节对话上下文记忆轮数】\n"
            f"当前记忆轮数: {current_hist} 轮\n"
            "每次对话向大模型发送的历史上下文长度："
        )
        histories = [
            (5, "5 轮 (节约 Token 极速响应)"),
            (10, "10 轮 (标准平衡推荐)"),
            (20, "20 轮 (深度长程连贯)"),
            (30, "30 轮 (超长对话沉浸)"),
        ]
        keyboard = []
        for h_val, h_label in histories:
            mark = "✓ " if current_hist == h_val else ""
            keyboard.append([InlineKeyboardButton(f"{mark}{h_label}", callback_data=f"set_history_{h_val}")])

        keyboard.append([InlineKeyboardButton("🔙 返回主控制台", callback_data="menu_main")])
        reply_markup = InlineKeyboardMarkup(keyboard) if HAS_TELEGRAM and InlineKeyboardMarkup else None
        return text, reply_markup

    async def build_metrics_menu(self) -> Tuple[str, Any]:
        """Constructs sub-menu for performance metrics and TTS cache control."""
        cache_stats = {"total_files": 0, "total_size_mb": 0.0, "total_hits": 0}
        metrics = {}
        try:
            async with get_db(self.db_path) as conn:
                cache_stats = await crud.get_tts_cache_stats(conn)
                metrics = await crud.get_metrics_overview(conn)
        except Exception as exc:
            logger.error("Database read failed in build_metrics_menu: %s", exc)

        total_files = cache_stats.get("total_files", 0)
        size_mb = cache_stats.get("total_size_mb", 0.0)
        hits = cache_stats.get("total_hits", 0)
        reqs = metrics.get("total_requests", 0)
        tokens = metrics.get("total_tokens", 0)
        cost_cny = metrics.get("estimated_cost_cny", 0.0)
        avg_ttft = metrics.get("avg_ttft_ms", 0.0)
        avg_tts = metrics.get("avg_tts_first_chunk_ms", 0.0)
        avg_tot = metrics.get("avg_total_latency_ms", 0.0)

        text = (
            "📊 【性能监控与语音缓存看板】\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💾 本地语音缓存 (TTS Cache):\n"
            f"• 缓存条数: {total_files} 个音频\n"
            f"• 占用空间: {size_mb} MB\n"
            f"• 命中次数: {hits} 次 (0延迟秒播)\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "⚡ 实时延迟指标 (Latency):\n"
            f"• 大模型首字延迟 (TTFT): {avg_ttft} ms\n"
            f"• 语音首包耗时 (TTS): {avg_tts} ms\n"
            f"• 全链路总耗时: {avg_tot} ms\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🪙 Token 与成本消耗:\n"
            f"• 累计请求: {reqs} 次 | 总 Token: {tokens}\n"
            f"• 预估成本: ¥{cost_cny}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💡 可点击下方按钮一键清理磁盘语音缓存："
        )

        keyboard = [
            [
                InlineKeyboardButton("🧹 清理本地语音缓存", callback_data="action_clear_cache"),
                InlineKeyboardButton("🔄 刷新性能数据", callback_data="menu_metrics"),
            ],
            [
                InlineKeyboardButton("🔙 返回主控制台", callback_data="menu_main"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard) if HAS_TELEGRAM and InlineKeyboardMarkup else None
        return text, reply_markup

    async def build_affection_menu(self, chat_id: int, user_id: int = 0) -> Tuple[str, Any]:
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
                        conn, user_id=str(user_id or chat_id), character_id=profile_id
                    )
                except Exception:
                    affection = None
                try:
                    msg_count = await crud.count_session_messages(conn, self._session_key(chat_id, user_id))
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
            "💡 发送 `/nickname <你的称呼>` 可随时更改夏目对你的专属称呼！"
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
        user_id = self._effective_user_id(update)

        if (data in ADMIN_CALLBACK_ACTIONS or data.startswith(ADMIN_CALLBACK_PREFIXES)) and not self._is_admin(update):
            logger.warning("Denied admin callback '%s' from user_id=%s in chat_id=%s", data, user_id, chat_id)
            if hasattr(query, "answer"):
                try:
                    await query.answer("⛔ 此操作需要管理员权限！", show_alert=True)
                except Exception:
                    pass
            return

        try:
            if data in ("menu_main", "menu_refresh"):
                text, markup = await self.build_main_console(chat_id, user_id)
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
                text, markup = await self.build_main_console(chat_id, user_id)
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data == "menu_tts":
                text, markup = await self.build_tts_menu()
                if hasattr(query, "answer"):
                    await query.answer()
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
                text, markup = await self.build_tts_menu()
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data == "menu_temp":
                text, markup = await self.build_temp_menu()
                if hasattr(query, "answer"):
                    await query.answer()
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data.startswith("set_temp_"):
                new_temp = float(data.split("_")[-1])
                try:
                    async with get_db(self.db_path) as conn:
                        await crud.update_settings(conn, SettingsUpdate(temperature=new_temp))
                except Exception as exc:
                    logger.warning("Temperature update exception: %s", exc)
                if hasattr(query, "answer"):
                    await query.answer(f"🌡️ 发音温度已设置为: {new_temp}", show_alert=True)
                text, markup = await self.build_tts_menu()
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data == "menu_split":
                text, markup = await self.build_split_menu()
                if hasattr(query, "answer"):
                    await query.answer()
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data.startswith("set_split_"):
                new_split = data.replace("set_split_", "")
                try:
                    async with get_db(self.db_path) as conn:
                        await crud.update_settings(conn, SettingsUpdate(text_split_method=new_split))
                except Exception as exc:
                    logger.warning("Split method update exception: %s", exc)
                if hasattr(query, "answer"):
                    await query.answer(f"✂️ 切分方式已设置为: {new_split}", show_alert=True)
                text, markup = await self.build_tts_menu()
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data == "menu_sampling":
                text, markup = await self.build_sampling_menu()
                if hasattr(query, "answer"):
                    await query.answer()
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data.startswith("set_topk_"):
                new_topk = int(data.split("_")[-1])
                try:
                    async with get_db(self.db_path) as conn:
                        await crud.update_settings(conn, SettingsUpdate(top_k=new_topk))
                except Exception as exc:
                    logger.warning("Top-K update exception: %s", exc)
                if hasattr(query, "answer"):
                    await query.answer(f"🎯 Top-K 已设置为: {new_topk}", show_alert=True)
                text, markup = await self.build_sampling_menu()
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data.startswith("set_topp_"):
                new_topp = float(data.split("_")[-1])
                try:
                    async with get_db(self.db_path) as conn:
                        await crud.update_settings(conn, SettingsUpdate(top_p=new_topp))
                except Exception as exc:
                    logger.warning("Top-P update exception: %s", exc)
                if hasattr(query, "answer"):
                    await query.answer(f"🎯 Top-P 已设置为: {new_topp}", show_alert=True)
                text, markup = await self.build_sampling_menu()
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data == "menu_batch":
                text, markup = await self.build_batch_menu()
                if hasattr(query, "answer"):
                    await query.answer()
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data.startswith("set_batch_"):
                new_batch = int(data.split("_")[-1])
                try:
                    async with get_db(self.db_path) as conn:
                        await crud.update_settings(conn, SettingsUpdate(batch_size=new_batch))
                except Exception as exc:
                    logger.warning("Batch update exception: %s", exc)
                if hasattr(query, "answer"):
                    await query.answer(f"📦 批量大小已设置为: {new_batch}", show_alert=True)
                text, markup = await self.build_tts_menu()
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data == "menu_interval":
                text, markup = await self.build_interval_menu()
                if hasattr(query, "answer"):
                    await query.answer()
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data.startswith("set_interval_"):
                new_interval = float(data.split("_")[-1])
                try:
                    async with get_db(self.db_path) as conn:
                        await crud.update_settings(conn, SettingsUpdate(fragment_interval=new_interval))
                except Exception as exc:
                    logger.warning("Interval update exception: %s", exc)
                if hasattr(query, "answer"):
                    await query.answer(f"⏱️ 分句连播间隔已设置为: {new_interval}s", show_alert=True)
                text, markup = await self.build_tts_menu()
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data == "menu_history":
                text, markup = await self.build_history_menu()
                if hasattr(query, "answer"):
                    await query.answer()
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data.startswith("set_history_"):
                new_hist = int(data.split("_")[-1])
                try:
                    async with get_db(self.db_path) as conn:
                        await crud.update_settings(conn, SettingsUpdate(max_history_messages=new_hist))
                except Exception as exc:
                    logger.warning("History update exception: %s", exc)
                if hasattr(query, "answer"):
                    await query.answer(f"🧠 记忆轮数已调整为: {new_hist} 轮", show_alert=True)
                text, markup = await self.build_main_console(chat_id, user_id)
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
                err_msg = None
                try:
                    async with get_db(self.db_path) as conn:
                        prov = await crud.get_provider_raw(conn, provider_id)
                        if not prov:
                            err_msg = "❌ 该模型提供商不存在！"
                        else:
                            prov_name = prov.name
                            has_key = bool(prov.api_key and prov.api_key.strip())
                            if provider_id != "custom" and not has_key:
                                err_msg = (
                                    f"⚠️ 无法激活 {prov_name}：未配置 API Key！\n\n"
                                    f"请先在管理网页端为 {prov_name} 填入有效 Key，或切换至已配置 Key 的模型。"
                                )
                            else:
                                await crud.set_active_provider(conn, provider_id)
                except Exception as exc:
                    logger.warning("Model switch exception: %s", exc)
                    err_msg = f"切换模型异常: {exc}"

                if err_msg:
                    if hasattr(query, "answer"):
                        await query.answer(err_msg, show_alert=True)
                    text, markup = await self.build_model_menu()
                    if hasattr(query, "edit_message_text"):
                        await query.edit_message_text(text=text, reply_markup=markup)
                else:
                    if hasattr(query, "answer"):
                        await query.answer(f"🤖 已激活大模型: {prov_name}", show_alert=True)
                    text, markup = await self.build_main_console(chat_id, user_id)
                    if hasattr(query, "edit_message_text"):
                        await query.edit_message_text(text=text, reply_markup=markup)

            elif data == "menu_metrics":
                text, markup = await self.build_metrics_menu()
                if hasattr(query, "answer"):
                    await query.answer("已刷新性能与缓存监控" if data == "menu_metrics" else None)
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data == "action_clear_cache":
                cleared_count = 0
                try:
                    async with get_db(self.db_path) as conn:
                        cleared_count = await crud.clear_all_tts_cache_entries(conn)
                except Exception as exc:
                    logger.warning("Clear cache exception: %s", exc)
                # Also evict cached audio files from disk, not just DB metadata.
                disk_cleared = 0
                try:
                    from galgame2voice.services.tts_cache_manager import get_tts_cache_manager
                    disk_cleared, _ = await get_tts_cache_manager().clear()
                except Exception as exc:
                    logger.warning("Disk cache clear exception: %s", exc)
                if hasattr(query, "answer"):
                    await query.answer(
                        f"🧹 本地语音缓存已清空 (清理了 {cleared_count} 条记录, {disk_cleared} 个磁盘文件)！",
                        show_alert=True,
                    )
                text, markup = await self.build_metrics_menu()
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data == "menu_affection":
                text, markup = await self.build_affection_menu(chat_id, user_id)
                if hasattr(query, "answer"):
                    await query.answer()
                if hasattr(query, "edit_message_text"):
                    await query.edit_message_text(text=text, reply_markup=markup)

            elif data == "action_reset":
                session_id = self._session_key(chat_id, user_id)
                self.cancel_user_task(chat_id)
                try:
                    async with get_db(self.db_path) as conn:
                        await crud.clear_session_messages(conn, session_id)
                except Exception as exc:
                    logger.warning("Could not clear session: %s", exc)
                if hasattr(query, "answer"):
                    await query.answer("🗑️ 当前会话记忆已清空！", show_alert=True)
                text, markup = await self.build_main_console(chat_id, user_id)
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
            "• /nickname <称呼> - 设置角色对你的专属称呼\n"
            "• /reset - 清空当前对话历史\n"
            "• /help - 查看完整帮助信息"
        )
        if hasattr(update, "message") and update.message:
            await update.message.reply_text(reply)
        elif hasattr(update, "effective_chat") and update.effective_chat and context and hasattr(context, "bot"):
            await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
        return reply

    async def handle_nickname(self, update: Any, context: Optional[Any] = None) -> str:
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
            new_nick = parts[1].strip()[:20]
            try:
                async with get_db(self.db_path) as conn:
                    profile = await crud.get_active_voice_profile(conn)
                    profile_id = profile.id if profile else 1
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
                reply = f"🌸 称呼已成功更新为「{new_nick}」！\n夏目在接下来的对话中就会这样称呼你啦~"
            except Exception as exc:
                logger.error("Failed to update nickname: %s", exc)
                reply = f"❌ 更新称呼失败: {exc}"

        if hasattr(update, "message") and update.message:
            await update.message.reply_text(reply)
        elif hasattr(update, "effective_chat") and update.effective_chat and context and hasattr(context, "bot"):
            await context.bot.send_message(chat_id=chat_id, text=reply)
        return reply

    async def handle_reset(self, update: Any, context: Optional[Any] = None) -> str:
        """Handler for /reset command."""
        chat_id = update.effective_chat.id if hasattr(update, "effective_chat") and update.effective_chat else 0
        session_id = self._session_key(chat_id, self._effective_user_id(update))
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
        text, markup = await self.build_main_console(chat_id, self._effective_user_id(update))

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
            "【支持的快捷指令】\n"
            "• /console - 打开原生交互控制台（音色/语速/模型切换）\n"
            "• /voice - 查看当前音色与语音设置\n"
            "• /model - 查看当前 LLM / STT 模型设置\n"
            "• /nickname <称呼> - 设置角色对你的专属称呼\n"
            "• /reset - 清空当前对话上下文\n"
            "• /help - 查看此帮助信息"
        )
        if hasattr(update, "message") and update.message:
            await update.message.reply_text(reply)
        elif hasattr(update, "effective_chat") and update.effective_chat and context and hasattr(context, "bot"):
            await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
        return reply

    async def handle_unknown(self, update: Any, context: Optional[Any] = None) -> str:
        """Handler for unknown commands."""
        reply = "未知指令，支持 /console, /voice, /model, /nickname, /reset, /help"
        if hasattr(update, "message") and update.message:
            await update.message.reply_text(reply)
        elif hasattr(update, "effective_chat") and update.effective_chat and context and hasattr(context, "bot"):
            await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
        return reply

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

        session_id = self._session_key(chat_id, effective_user_id)

        # 2. Query ChatService / LLM Adapter for bilingual response
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
                    await self.chat_service.memory_service.extract_and_save_facts(
                        user_id=str(effective_user_id),
                        text=text,
                        character_id=profile_id,
                        conn=conn,
                    )
            except Exception as mem_err:
                logger.debug("Telegram non-critical memory extraction exception: %s", mem_err)

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

            # 4. Immediately persist assistant message to DB & process affection progression
            async with get_db(self.db_path) as conn:
                await crud.add_message(conn, MessageCreate(
                    session_id=session_id,
                    role="assistant",
                    content_chinese=chinese,
                    content_japanese=japanese,
                    audio_url="",
                    latency_ms=0,
                ))
                try:
                    if hasattr(self.chat_service, "affection_service"):
                        await self.chat_service.affection_service.process_turn(
                            user_id=str(effective_user_id),
                            character_id=profile_id,
                            user_text=text,
                            bot_text=chinese,
                            conn=conn,
                        )
                except Exception as aff_err:
                    logger.debug("Telegram non-critical affection update exception: %s", aff_err)

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
        return await self.process_text_chat(chat_id, text, context.bot, user_id=self._effective_user_id(update))

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
            return await self.process_text_chat(chat_id, transcribed_text.strip(), context.bot, user_id=self._effective_user_id(update))

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
