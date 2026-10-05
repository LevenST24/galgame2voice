"""
Telegram Interactive Console Menus and Callback Router for galgame2voice.

Provides rich inline keyboard menus and callback processing for:
- Main Interactive Console (/console)
- Voice Profile and Character Switcher (/character, /voice)
- Large Language Model Switcher (/model)
- Advanced Text-to-Speech (TTS) Tuning (speed, temperature, split, sampling, batch, interval)
- Context History Memory Window
- Character Affection Profile
- Performance & Latency Metrics Dashboard and Audio Cache Cleanup
"""

from dataclasses import dataclass
import logging
from typing import Any, Callable

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    HAS_TELEGRAM = True
except ImportError:
    HAS_TELEGRAM = False
    InlineKeyboardButton = Any
    InlineKeyboardMarkup = Any

from galgame2voice.database.session import get_db
from galgame2voice.database import crud
from galgame2voice.database.models import SettingsUpdate
from galgame2voice.utils.logger import sanitize_error_detail

logger = logging.getLogger("galgame2voice.telegram_bot.console_menus")

# Callback data that mutates global state and therefore requires admin privileges.
# Via handlers the whitelist is fail-closed (empty = nobody); only the handlers-less
# path treats an empty whitelist as open access (single-user setups).
ADMIN_CALLBACK_PREFIXES: tuple[str, ...] = (
    "set_voice_", "set_char_", "set_speed_", "set_temp_", "set_split_", "set_topk_",
    "set_topp_", "set_batch_", "set_interval_", "set_history_", "set_model_",
)
ADMIN_CALLBACK_ACTIONS: set[str] = {"action_clear_cache"}


def resolve_effective_user_id(update: Any) -> int:
    """Resolves the individual Telegram user behind an update (0 if unknown)."""
    user = getattr(update, "effective_user", None)
    uid = getattr(user, "id", None) if user else None
    return int(uid) if uid else 0


def check_is_admin(update: Any, admin_ids: set[int] | None = None) -> bool:
    """Checks whether the effective user is authorized as an administrator."""
    if not admin_ids:
        return True
    return resolve_effective_user_id(update) in admin_ids


def resolve_session_key(chat_id: int, user_id: int = 0) -> str:
    """
    Computes session key for given chat_id and user_id.
    Private chats (user_id == chat_id or unknown) keep the legacy per-chat key
    so existing history survives; group chats append the member's user id
    so each member gets private history/memory state.
    """
    if not user_id or user_id == chat_id:
        return f"tg_{chat_id}"
    return f"tg_{chat_id}_{user_id}"


def _resolve_menu_context(
    chat_id: Any = 0,
    user_id: int = 0,
    db_path: str | None = None,
    session_key_fn: Callable[[int, int], str] | None = None,
) -> tuple[str | None, int, int, str]:
    """Resolves (actual_db_path, actual_chat_id, actual_user_id, session_key) for interactive menus."""
    if isinstance(chat_id, str) and not chat_id.isdigit():
        actual_db_path = chat_id
        actual_chat_id = user_id
        actual_user_id = 0
    else:
        actual_db_path = db_path
        actual_chat_id = int(chat_id or 0)
        actual_user_id = int(user_id or 0)

    key_fn = session_key_fn or resolve_session_key
    session_key = key_fn(actual_chat_id, actual_user_id)
    return actual_db_path, actual_chat_id, actual_user_id, session_key


async def _get_affection_safe(conn, user_id_key: str, profile: Any) -> Any | None:
    """Safely retrieves or initializes character affection without throwing on errors."""
    profile_id = profile.id if profile else 1
    try:
        return await crud.get_or_create_character_affection(
            conn, user_id=user_id_key, character_id=profile_id
        )
    except Exception:
        return None


async def _get_setting_field(
    db_path: str | None,
    field_name: str,
    default: Any,
    caller_name: str,
) -> Any:
    """Reads a single scalar field from settings with fallback and error logging."""
    try:
        async with get_db(db_path) as conn:
            settings = await crud.get_settings_raw(conn)
            return getattr(settings, field_name, default)
    except Exception as exc:
        logger.error("Database read failed in %s: %s", caller_name, exc)
        return default


def _tts_nav_row() -> list:
    """Constructs navigation row linking back to TTS tuning menu and main console."""
    return [
        InlineKeyboardButton("🔙 返回语音调参", callback_data="menu_tts"),
        InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main"),
    ]


def _make_inline_markup(keyboard: list[list[Any]] | None) -> Any | None:
    """Wraps button rows into InlineKeyboardMarkup if telegram package is available and keyboard is non-empty."""
    if not (HAS_TELEGRAM and InlineKeyboardMarkup and keyboard):
        return None
    return InlineKeyboardMarkup(keyboard)


def _build_single_col_options_markup(
    options: list[tuple[Any, str]],
    current_val: Any,
    callback_prefix: str,
    nav_row: list | None = None,
    is_float: bool = False,
) -> Any:
    """Builds a single-column inline keyboard for selectable scalar options."""
    if not (HAS_TELEGRAM and InlineKeyboardButton):
        return None
    keyboard = []
    for val, label in options:
        if is_float and isinstance(current_val, (int, float)) and isinstance(val, (int, float)):
            is_active = abs(current_val - val) < 0.01
        else:
            is_active = (current_val == val)
        mark = "✓ " if is_active else ""
        keyboard.append([InlineKeyboardButton(f"{mark}{label}", callback_data=f"{callback_prefix}{val}")])
    if nav_row:
        keyboard.append(nav_row)
    return _make_inline_markup(keyboard)


async def build_main_console(
    chat_id: Any = 0,
    user_id: int = 0,
    db_path: str | None = None,
    session_key_fn: Callable[[int, int], str] | None = None,
) -> tuple[str, Any]:
    """Constructs the rich text and inline keyboard for the Telegram Interactive Console."""
    actual_db_path, actual_chat_id, actual_user_id, session_key = _resolve_menu_context(
        chat_id, user_id, db_path, session_key_fn
    )

    profile = None
    settings = None
    provider = None
    affection = None
    msg_count = 0
    cache_stats = {"total_files": 0, "total_size_mb": 0.0}

    try:
        async with get_db(actual_db_path) as conn:
            profile = await crud.get_active_voice_profile(conn)
            settings = await crud.get_settings_raw(conn)
            provider = await crud.get_active_provider(conn, mask=True)
            affection = await _get_affection_safe(conn, str(actual_user_id or actual_chat_id), profile)
            try:
                msg_count = await crud.count_session_messages(conn, session_key)
            except Exception:
                msg_count = 0
            try:
                cache_stats = await crud.get_tts_cache_stats(conn)
            except Exception as cache_err:
                logger.debug("Failed reading cache stats in build_main_console: %s", cache_err)
    except Exception as exc:
        logger.debug("Database read failed in build_main_console: %s", exc)

    char_name = profile.name if profile else "未配置角色"
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
        reply_markup = _make_inline_markup(keyboard)

    return text, reply_markup


async def build_voice_menu(db_path: str | None = None) -> tuple[str, Any]:
    """Constructs rich sub-menu for switching voice profiles with 2-column layout and active character card."""
    profiles = []
    active_id = None
    try:
        async with get_db(db_path) as conn:
            profiles = await crud.list_voice_profiles(conn)
            active = await crud.get_active_voice_profile(conn)
            active_id = active.id if active else None
    except Exception as exc:
        logger.error("Database read failed in build_voice_menu: %s", exc)

    active_prof = next((p for p in profiles if active_id is not None and p.id == active_id), None)
    active_name = active_prof.name if active_prof else (profiles[0].name if profiles else "未配置角色")
    active_desc = active_prof.description if (active_prof and active_prof.description) else "暂无角色简介"
    if len(active_desc) > 60:
        active_desc = active_desc[:57] + "..."
    active_lang = f"{active_prof.prompt_lang} / {active_prof.text_lang}" if active_prof else "ja / zh"

    text = (
        "🎭 【选择角色音色 · 二次元伴侣中心】\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🌸 当前伴侣: {active_name}\n"
        f"📝 角色简介: {active_desc}\n"
        f"🎙️ 默认语言: {active_lang}\n"
        f"📚 收录角色: 共 {len(profiles)} 位伴侣\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💡 请点击下方按钮切换你想对话的角色："
    )

    keyboard = []
    if HAS_TELEGRAM and InlineKeyboardButton and InlineKeyboardMarkup:
        other_btns = []
        for p in profiles:
            is_cur = (active_id is not None and p.id == active_id)
            if is_cur:
                keyboard.append([InlineKeyboardButton(f"🌸 {p.name} (当前)", callback_data=f"set_voice_{p.id}")])
            else:
                other_btns.append(InlineKeyboardButton(f"▫️ {p.name}", callback_data=f"set_voice_{p.id}"))

        # Lay out non-active characters in a clean 2-column grid
        for i in range(0, len(other_btns), 2):
            keyboard.append(other_btns[i : i + 2])

        keyboard.append([
            InlineKeyboardButton("🔄 刷新角色", callback_data="menu_voice"),
            InlineKeyboardButton("🔙 返回主控制台", callback_data="menu_main"),
        ])

    reply_markup = _make_inline_markup(keyboard)
    return text, reply_markup


async def build_model_menu(db_path: str | None = None) -> tuple[str, Any]:
    """Constructs sub-menu for switching active LLM provider with API key safety indicators."""
    providers = []
    active_id = None
    try:
        async with get_db(db_path) as conn:
            providers = await crud.list_providers(conn, mask=True)
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
    reply_markup = _make_inline_markup(keyboard)
    return text, reply_markup


async def build_tts_menu(db_path: str | None = None) -> tuple[str, Any]:
    """Constructs sub-menu for advanced TTS parameters."""
    settings = None
    try:
        async with get_db(db_path) as conn:
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
    reply_markup = _make_inline_markup(keyboard)
    return text, reply_markup


async def build_speed_menu(db_path: str | None = None) -> tuple[str, Any]:
    """Constructs sub-menu for adjusting voice speed factor."""
    current_speed = await _get_setting_field(db_path, "speed_factor", 1.05, "build_speed_menu")

    text = f"⚡ 【调节语音语速】\n当前语速: {current_speed}x\n请选择你期望的发音语速："
    speeds = [0.8, 0.9, 1.0, 1.05, 1.1, 1.2, 1.3, 1.5]
    def _speed_btn(s: float) -> Any:
        mark = "✓ " if abs(current_speed - s) < 0.01 else ""
        return InlineKeyboardButton(f"{mark}{s}x", callback_data=f"set_speed_{s}")

    keyboard = [
        [_speed_btn(s) for s in speeds[:4]],
        [_speed_btn(s) for s in speeds[4:]],
        _tts_nav_row(),
    ]
    reply_markup = _make_inline_markup(keyboard)
    return text, reply_markup


async def build_temp_menu(db_path: str | None = None) -> tuple[str, Any]:
    """Constructs sub-menu for adjusting voice temperature."""
    current_temp = await _get_setting_field(db_path, "temperature", 0.8, "build_temp_menu")

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
    reply_markup = _build_single_col_options_markup(
        temps, current_temp, "set_temp_", nav_row=_tts_nav_row(), is_float=True
    )
    return text, reply_markup


async def build_split_menu(db_path: str | None = None) -> tuple[str, Any]:
    """Constructs sub-menu for text split method."""
    current_split = await _get_setting_field(db_path, "text_split_method", "cut5", "build_split_menu")

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
    reply_markup = _build_single_col_options_markup(
        splits, current_split, "set_split_", nav_row=_tts_nav_row(), is_float=False
    )
    return text, reply_markup


async def build_sampling_menu(db_path: str | None = None) -> tuple[str, Any]:
    """Constructs sub-menu for Top-K and Top-P sampling parameters."""
    top_k = 15
    top_p = 1.0
    try:
        async with get_db(db_path) as conn:
            settings = await crud.get_settings_raw(conn)
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
        _tts_nav_row(),
    ]
    reply_markup = _make_inline_markup(keyboard)
    return text, reply_markup


async def build_batch_menu(db_path: str | None = None) -> tuple[str, Any]:
    """Constructs sub-menu for batch size."""
    current_batch = await _get_setting_field(db_path, "batch_size", 1, "build_batch_menu")

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
    reply_markup = _build_single_col_options_markup(
        batches, current_batch, "set_batch_", nav_row=_tts_nav_row(), is_float=False
    )
    return text, reply_markup


async def build_interval_menu(db_path: str | None = None) -> tuple[str, Any]:
    """Constructs sub-menu for fragment interval."""
    current_interval = await _get_setting_field(db_path, "fragment_interval", 0.3, "build_interval_menu")

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
    reply_markup = _build_single_col_options_markup(
        intervals, current_interval, "set_interval_", nav_row=_tts_nav_row(), is_float=True
    )
    return text, reply_markup


async def build_history_menu(db_path: str | None = None) -> tuple[str, Any]:
    """Constructs sub-menu for conversational memory history length."""
    current_hist = await _get_setting_field(db_path, "max_history_messages", 10, "build_history_menu")

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
    nav_row = [InlineKeyboardButton("🔙 返回主控制台", callback_data="menu_main")]
    reply_markup = _build_single_col_options_markup(
        histories, current_hist, "set_history_", nav_row=nav_row, is_float=False
    )
    return text, reply_markup


async def build_metrics_menu(db_path: str | None = None) -> tuple[str, Any]:
    """Constructs sub-menu for performance metrics and TTS cache control."""
    cache_stats = {"total_files": 0, "total_size_mb": 0.0, "total_hits": 0}
    metrics: dict[str, Any] = {}
    try:
        async with get_db(db_path) as conn:
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
    reply_markup = _make_inline_markup(keyboard)
    return text, reply_markup


async def build_affection_menu(
    chat_id: Any = 0,
    user_id: int = 0,
    db_path: str | None = None,
    session_key_fn: Callable[[int, int], str] | None = None,
) -> tuple[str, Any]:
    """Constructs sub-menu for displaying affection details and emotion."""
    actual_db_path, actual_chat_id, actual_user_id, session_key = _resolve_menu_context(
        chat_id, user_id, db_path, session_key_fn
    )

    profile = None
    affection = None
    msg_count = 0
    try:
        async with get_db(actual_db_path) as conn:
            profile = await crud.get_active_voice_profile(conn)
            affection = await _get_affection_safe(conn, str(actual_user_id or actual_chat_id), profile)
            try:
                msg_count = await crud.count_session_messages(conn, session_key)
            except Exception:
                msg_count = 0
    except Exception as exc:
        logger.debug("Database read failed in build_affection_menu: %s", exc)

    char_name = profile.name if profile else "未配置角色"
    clean_name = char_name.split("(")[0].strip() if profile else "伴侣"
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
        f"💡 发送 `/nickname <你的称呼>` 可随时更改{clean_name}对你的专属称呼！"
    )
    keyboard = [
        [
            InlineKeyboardButton("🎭 切换其他角色", callback_data="menu_voice"),
            InlineKeyboardButton("🔄 刷新好感档案", callback_data="menu_affection"),
        ],
        [InlineKeyboardButton("🔙 返回主控制台", callback_data="menu_main")],
    ]
    reply_markup = _make_inline_markup(keyboard)
    return text, reply_markup


@dataclass
class _CallbackContext:
    data: str
    query: Any
    chat_id: int
    user_id: int
    db_path: str | None
    session_key_fn: Callable[[int, int], str]
    cancel_task: Callable[[int], None]


async def _render_menu(
    ctx: _CallbackContext,
    menu_coro: Any,
    answer_text: str | None = None,
) -> None:
    """Executes a menu builder coroutine and safely updates the Telegram message text and markup."""
    text, markup = await menu_coro
    if hasattr(ctx.query, "answer"):
        await ctx.query.answer(answer_text)
    if hasattr(ctx.query, "edit_message_text"):
        await ctx.query.edit_message_text(text=text, reply_markup=markup)


async def _edit_menu_text(
    ctx: _CallbackContext,
    menu_coro: Any,
) -> None:
    """Executes a menu builder coroutine and updates message text/markup without answering the query."""
    text, markup = await menu_coro
    if hasattr(ctx.query, "edit_message_text"):
        await ctx.query.edit_message_text(text=text, reply_markup=markup)


def _build_main_console_for_ctx(ctx: _CallbackContext) -> Any:
    """Builds the main console coroutine configured with the callback context's chat/user/db identity."""
    return build_main_console(
        chat_id=ctx.chat_id, user_id=ctx.user_id, db_path=ctx.db_path, session_key_fn=ctx.session_key_fn
    )


async def _return_to_main_console(ctx: _CallbackContext) -> None:
    """Updates Telegram message text/markup to the main console."""
    await _edit_menu_text(ctx, _build_main_console_for_ctx(ctx))


async def _handle_main_menu(ctx: _CallbackContext) -> None:
    await _render_menu(
        ctx,
        _build_main_console_for_ctx(ctx),
        answer_text="已刷新控制台" if ctx.data == "menu_refresh" else None,
    )


async def _handle_voice_menu(ctx: _CallbackContext) -> None:
    await _render_menu(ctx, build_voice_menu(db_path=ctx.db_path))


async def _handle_set_voice(ctx: _CallbackContext) -> None:
    raw_id = ctx.data.replace("set_voice_", "").replace("set_char_", "").strip()
    if not raw_id.isdigit() or int(raw_id) < 1:
        if hasattr(ctx.query, "answer"):
            await ctx.query.answer("⚠️ 无效的角色音色 ID", show_alert=True)
        return
    profile_id = int(raw_id)
    # Already-active guard: same source of truth as build_voice_menu's
    # "(当前)" marker. Fail-open: on any DB error, fall through to switch.
    _active_id = None
    try:
        async with get_db(ctx.db_path) as conn:
            _db_active = await crud.get_active_voice_profile(conn)
            if _db_active is not None:
                _active_id = getattr(_db_active, "id", None)
    except Exception as exc:
        logger.debug("Failed getting active voice profile in handle_select_character: %s", exc)
    if _active_id is not None and profile_id == _active_id:
        if hasattr(ctx.query, "answer"):
            await ctx.query.answer("已经是当前音色，无需切换")
        return
    char_name = "目标角色"
    err_msg = None
    warning_note = ""
    try:
        from galgame2voice.services.voice_manager import get_voice_manager, InsufficientMemoryError
        vm = get_voice_manager()
        try:
            switched = await vm.switch_active_profile(profile_id)
            if not switched:
                warning_note = "（语音引擎离线）"
        except InsufficientMemoryError:
            raise
        except Exception as sw_err:
            logger.debug("VoiceManager weight switch skipped: %s", sw_err)
            warning_note = "（语音引擎离线）"

        async with get_db(ctx.db_path) as conn:
            await crud.set_active_voice_profile(conn, profile_id)
            profile = await crud.get_voice_profile(conn, profile_id)
            if profile:
                char_name = profile.name
    except InsufficientMemoryError as mem_err:
        err_msg = f"系统内存不足，无法加载该模型: {mem_err}"
        logger.warning("Insufficient memory switching to profile %d: %s", profile_id, mem_err)
    except Exception as exc:
        err_msg = f"切换异常: {sanitize_error_detail(exc)}"
        logger.warning("Voice switch exception for profile %d: %s", profile_id, exc)

    if err_msg:
        if hasattr(ctx.query, "answer"):
            await ctx.query.answer(f"⚠️ {err_msg}", show_alert=True)
        await _edit_menu_text(ctx, build_voice_menu(db_path=ctx.db_path))
    else:
        if hasattr(ctx.query, "answer"):
            await ctx.query.answer(f"🌸 音色已切换为: {char_name}{warning_note}", show_alert=True)
        await _return_to_main_console(ctx)


async def _handle_tts_menu(ctx: _CallbackContext) -> None:
    await _render_menu(ctx, build_tts_menu(db_path=ctx.db_path))


async def _handle_speed_menu(ctx: _CallbackContext) -> None:
    await _render_menu(ctx, build_speed_menu(db_path=ctx.db_path))


async def _handle_scalar_setting(
    ctx: _CallbackContext,
    prefix: str,
    parser: Callable[[str], Any],
    val_min: int | float,
    val_max: int | float,
    range_msg: str,
    setting_key: str,
    log_name: str,
    success_tmpl: str,
    menu_builder: Callable[[], Any],
) -> None:
    raw = ctx.data.replace(prefix, "").strip()
    try:
        new_val = parser(raw)
        if not (val_min <= new_val <= val_max):
            raise ValueError()
    except (ValueError, TypeError):
        if hasattr(ctx.query, "answer"):
            await ctx.query.answer(range_msg, show_alert=True)
        return
    try:
        async with get_db(ctx.db_path) as conn:
            await crud.update_settings(conn, SettingsUpdate(**{setting_key: new_val}))
    except Exception as exc:
        logger.warning("%s update exception: %s", log_name, exc)
    if hasattr(ctx.query, "answer"):
        await ctx.query.answer(success_tmpl.format(val=new_val), show_alert=True)
    await _edit_menu_text(ctx, menu_builder())


async def _handle_set_speed(ctx: _CallbackContext) -> None:
    await _handle_scalar_setting(
        ctx,
        prefix="set_speed_",
        parser=float,
        val_min=0.1,
        val_max=3.0,
        range_msg="⚠️ 语速参数超出范围 (0.1~3.0)",
        setting_key="speed_factor",
        log_name="Speed",
        success_tmpl="⚡ 语速已调整为: {val}x",
        menu_builder=lambda: build_tts_menu(db_path=ctx.db_path),
    )


async def _handle_temp_menu(ctx: _CallbackContext) -> None:
    await _render_menu(ctx, build_temp_menu(db_path=ctx.db_path))


async def _handle_set_temp(ctx: _CallbackContext) -> None:
    await _handle_scalar_setting(
        ctx,
        prefix="set_temp_",
        parser=float,
        val_min=0.0,
        val_max=2.0,
        range_msg="⚠️ 发音温度超出范围 (0.0~2.0)",
        setting_key="temperature",
        log_name="Temperature",
        success_tmpl="🌡️ 发音温度已设置为: {val}",
        menu_builder=lambda: build_tts_menu(db_path=ctx.db_path),
    )


async def _handle_split_menu(ctx: _CallbackContext) -> None:
    await _render_menu(ctx, build_split_menu(db_path=ctx.db_path))


async def _handle_set_split(ctx: _CallbackContext) -> None:
    new_split = ctx.data.replace("set_split_", "")
    try:
        async with get_db(ctx.db_path) as conn:
            await crud.update_settings(conn, SettingsUpdate(text_split_method=new_split))
    except Exception as exc:
        logger.warning("Split method update exception: %s", exc)
    if hasattr(ctx.query, "answer"):
        await ctx.query.answer(f"✂️ 切分方式已设置为: {new_split}", show_alert=True)
    await _edit_menu_text(ctx, build_tts_menu(db_path=ctx.db_path))


async def _handle_sampling_menu(ctx: _CallbackContext) -> None:
    await _render_menu(ctx, build_sampling_menu(db_path=ctx.db_path))


async def _handle_set_topk(ctx: _CallbackContext) -> None:
    await _handle_scalar_setting(
        ctx,
        prefix="set_topk_",
        parser=int,
        val_min=1,
        val_max=100,
        range_msg="⚠️ Top-K 参数超出范围 (1~100)",
        setting_key="top_k",
        log_name="Top-K",
        success_tmpl="🎯 Top-K 已设置为: {val}",
        menu_builder=lambda: build_sampling_menu(db_path=ctx.db_path),
    )


async def _handle_set_topp(ctx: _CallbackContext) -> None:
    await _handle_scalar_setting(
        ctx,
        prefix="set_topp_",
        parser=float,
        val_min=0.0,
        val_max=1.0,
        range_msg="⚠️ Top-P 参数超出范围 (0.0~1.0)",
        setting_key="top_p",
        log_name="Top-P",
        success_tmpl="🎯 Top-P 已设置为: {val}",
        menu_builder=lambda: build_sampling_menu(db_path=ctx.db_path),
    )


async def _handle_batch_menu(ctx: _CallbackContext) -> None:
    await _render_menu(ctx, build_batch_menu(db_path=ctx.db_path))


async def _handle_set_batch(ctx: _CallbackContext) -> None:
    await _handle_scalar_setting(
        ctx,
        prefix="set_batch_",
        parser=int,
        val_min=1,
        val_max=16,
        range_msg="⚠️ 批量大小超出范围 (1~16)",
        setting_key="batch_size",
        log_name="Batch",
        success_tmpl="📦 批量大小已设置为: {val}",
        menu_builder=lambda: build_tts_menu(db_path=ctx.db_path),
    )


async def _handle_interval_menu(ctx: _CallbackContext) -> None:
    await _render_menu(ctx, build_interval_menu(db_path=ctx.db_path))


async def _handle_set_interval(ctx: _CallbackContext) -> None:
    await _handle_scalar_setting(
        ctx,
        prefix="set_interval_",
        parser=float,
        val_min=0.0,
        val_max=5.0,
        range_msg="⚠️ 分句间隔超出范围 (0.0~5.0s)",
        setting_key="fragment_interval",
        log_name="Interval",
        success_tmpl="⏱️ 分句连播间隔已设置为: {val}s",
        menu_builder=lambda: build_tts_menu(db_path=ctx.db_path),
    )


async def _handle_history_menu(ctx: _CallbackContext) -> None:
    await _render_menu(ctx, build_history_menu(db_path=ctx.db_path))


async def _handle_set_history(ctx: _CallbackContext) -> None:
    await _handle_scalar_setting(
        ctx,
        prefix="set_history_",
        parser=int,
        val_min=1,
        val_max=100,
        range_msg="⚠️ 记忆轮数超出范围 (1~100)",
        setting_key="max_history_messages",
        log_name="History",
        success_tmpl="🧠 记忆轮数已调整为: {val} 轮",
        menu_builder=lambda: _build_main_console_for_ctx(ctx),
    )


async def _handle_model_menu(ctx: _CallbackContext) -> None:
    await _render_menu(ctx, build_model_menu(db_path=ctx.db_path))


async def _handle_set_model(ctx: _CallbackContext) -> None:
    provider_id = ctx.data.replace("set_model_", "").strip()
    if not provider_id or len(provider_id) > 64:
        if hasattr(ctx.query, "answer"):
            await ctx.query.answer("⚠️ 无效的模型提供商标识", show_alert=True)
        return
    prov_name = provider_id
    err_msg = None
    try:
        async with get_db(ctx.db_path) as conn:
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
        logger.warning("Model switch exception for provider '%s': %s", provider_id, exc)
        err_msg = f"切换模型异常: {exc}"

    if err_msg:
        if hasattr(ctx.query, "answer"):
            await ctx.query.answer(err_msg, show_alert=True)
        await _edit_menu_text(ctx, build_model_menu(db_path=ctx.db_path))
    else:
        if hasattr(ctx.query, "answer"):
            await ctx.query.answer(f"🤖 已激活大模型: {prov_name}", show_alert=True)
        await _return_to_main_console(ctx)


async def _handle_metrics_menu(ctx: _CallbackContext) -> None:
    await _render_menu(
        ctx,
        build_metrics_menu(db_path=ctx.db_path),
        answer_text="已刷新性能与缓存监控" if ctx.data == "menu_metrics" else None,
    )


async def _handle_clear_cache(ctx: _CallbackContext) -> None:
    cleared_count = 0
    try:
        async with get_db(ctx.db_path) as conn:
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
    if hasattr(ctx.query, "answer"):
        await ctx.query.answer(
            f"🧹 本地语音缓存已清空 (清理了 {cleared_count} 条记录, {disk_cleared} 个磁盘文件)！",
            show_alert=True,
        )
    await _edit_menu_text(ctx, build_metrics_menu(db_path=ctx.db_path))


async def _handle_affection_menu(ctx: _CallbackContext) -> None:
    await _render_menu(
        ctx,
        build_affection_menu(
            chat_id=ctx.chat_id, user_id=ctx.user_id, db_path=ctx.db_path, session_key_fn=ctx.session_key_fn
        ),
    )


async def _handle_reset_session(ctx: _CallbackContext) -> None:
    session_id = ctx.session_key_fn(ctx.chat_id, ctx.user_id)
    ctx.cancel_task(ctx.chat_id)
    try:
        async with get_db(ctx.db_path) as conn:
            await crud.clear_session_messages(conn, session_id)
    except Exception as exc:
        logger.warning("Could not clear session %s: %s", session_id, exc)
    if hasattr(ctx.query, "answer"):
        await ctx.query.answer("🗑️ 当前会话记忆已清空！", show_alert=True)
    await _return_to_main_console(ctx)


_EXACT_CALLBACK_HANDLERS: dict[str, Callable] = {
    "menu_main": _handle_main_menu,
    "menu_refresh": _handle_main_menu,
    "menu_voice": _handle_voice_menu,
    "menu_tts": _handle_tts_menu,
    "menu_speed": _handle_speed_menu,
    "menu_temp": _handle_temp_menu,
    "menu_split": _handle_split_menu,
    "menu_sampling": _handle_sampling_menu,
    "menu_batch": _handle_batch_menu,
    "menu_interval": _handle_interval_menu,
    "menu_history": _handle_history_menu,
    "menu_model": _handle_model_menu,
    "menu_metrics": _handle_metrics_menu,
    "action_clear_cache": _handle_clear_cache,
    "menu_affection": _handle_affection_menu,
    "action_reset": _handle_reset_session,
}

_PREFIX_CALLBACK_HANDLERS: tuple[tuple[tuple[str, ...], Callable], ...] = (
    (("set_voice_", "set_char_"), _handle_set_voice),
    (("set_speed_",), _handle_set_speed),
    (("set_temp_",), _handle_set_temp),
    (("set_split_",), _handle_set_split),
    (("set_topk_",), _handle_set_topk),
    (("set_topp_",), _handle_set_topp),
    (("set_batch_",), _handle_set_batch),
    (("set_interval_",), _handle_set_interval),
    (("set_history_",), _handle_set_history),
    (("set_model_",), _handle_set_model),
)


def _extract_callback_caller_info(
    handlers_or_update: Any,
    update_or_context: Any | None,
    context: Any | None,
    admin_ids: set[int] | None,
) -> tuple[Any, Any, Any, int, int, bool, str]:
    """Extracts normalized handlers, update, query, chat_id, user_id, is_admin, and callback data."""
    if hasattr(handlers_or_update, "callback_query") or (
        hasattr(handlers_or_update, "effective_chat") and not hasattr(handlers_or_update, "user_tasks")
    ):
        handlers = None
        update = handlers_or_update
    else:
        handlers = handlers_or_update
        update = update_or_context

    query = getattr(update, "callback_query", None) if update else None
    data = (getattr(query, "data", "") or "") if query else ""
    chat_id = update.effective_chat.id if hasattr(update, "effective_chat") and update.effective_chat else 0

    if handlers and hasattr(handlers, "_effective_user_id"):
        user_id = handlers._effective_user_id(update)
    else:
        user_id = resolve_effective_user_id(update)

    effective_admin_ids = (
        admin_ids if admin_ids is not None
        else (getattr(handlers, "admin_ids", set()) if handlers else set())
    )

    if handlers and hasattr(handlers, "_is_admin"):
        is_admin = handlers._is_admin(update)
    else:
        is_admin = check_is_admin(update, effective_admin_ids)

    return handlers, update, query, chat_id, user_id, is_admin, data


async def route_callback_query(
    handlers_or_update: Any,
    update_or_context: Any | None = None,
    context: Any | None = None,
    *,
    db_path: str | None = None,
    admin_ids: set[int] | None = None,
) -> None:
    """
    Routes and handles inline button clicks in Telegram.
    Accepts either:
    - (handlers, update, context) when called from TelegramBotHandlers method
    - (update, context, db_path=..., admin_ids=...) when called directly
    """
    (
        handlers,
        _,
        query,
        chat_id,
        user_id,
        is_admin,
        data,
    ) = _extract_callback_caller_info(handlers_or_update, update_or_context, context, admin_ids)

    if not query:
        return

    if (data in ADMIN_CALLBACK_ACTIONS or data.startswith(ADMIN_CALLBACK_PREFIXES)) and not is_admin:
        logger.warning("Denied admin callback '%s' from user_id=%s in chat_id=%s", data, user_id, chat_id)
        if hasattr(query, "answer"):
            try:
                await query.answer("⛔ 此操作需要管理员权限！", show_alert=True)
            except Exception as ans_err:
                logger.debug("Failed answering admin callback query: %s", ans_err)
        return

    actual_db_path = (
        db_path if db_path is not None
        else (getattr(handlers, "db_path", None) if handlers else None)
    )

    def cancel_task(cid: int) -> None:
        if handlers and hasattr(handlers, "cancel_user_task"):
            handlers.cancel_user_task(cid)

    def session_key_fn(cid: int, uid: int) -> str:
        if handlers and hasattr(handlers, "_session_key"):
            return handlers._session_key(cid, uid)
        return resolve_session_key(cid, uid)

    try:
        ctx = _CallbackContext(data=data, query=query, chat_id=chat_id, user_id=user_id,
                               db_path=actual_db_path, session_key_fn=session_key_fn, cancel_task=cancel_task)
        handler = _EXACT_CALLBACK_HANDLERS.get(data)
        if handler is None:
            for prefixes, prefix_handler in _PREFIX_CALLBACK_HANDLERS:
                if data.startswith(prefixes):
                    handler = prefix_handler
                    break
        if handler is not None:
            await handler(ctx)
        # unknown callback_data: no handler matched, so the query is left unacknowledged
    except Exception as exc:
        logger.error("Error processing callback query '%s': %s", data, exc, exc_info=True)
        if hasattr(query, "answer"):
            try:
                safe_err = sanitize_error_detail(exc)
                await query.answer(f"操作异常: {safe_err}" if safe_err else "操作异常，请重试", show_alert=True)
            except Exception as ans_err:
                logger.debug("Failed answering error callback query: %s", ans_err)


# Alias for backward-compatibility and clean imports
handle_callback_query = route_callback_query

__all__ = [
    "ADMIN_CALLBACK_PREFIXES",
    "ADMIN_CALLBACK_ACTIONS",
    "HAS_TELEGRAM",
    "resolve_effective_user_id",
    "check_is_admin",
    "resolve_session_key",
    "build_main_console",
    "build_voice_menu",
    "build_model_menu",
    "build_tts_menu",
    "build_speed_menu",
    "build_temp_menu",
    "build_split_menu",
    "build_sampling_menu",
    "build_batch_menu",
    "build_interval_menu",
    "build_history_menu",
    "build_metrics_menu",
    "build_affection_menu",
    "route_callback_query",
    "handle_callback_query",
]
