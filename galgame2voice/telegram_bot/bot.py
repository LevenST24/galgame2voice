"""
Telegram Bot Async Lifecycle and Application Manager.
Manages python-telegram-bot Application instance, token validation, polling, and shutdown.
"""

import asyncio
import logging
import os
from typing import Any

import httpx

try:
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters
    from telegram.request import HTTPXRequest
    HAS_TELEGRAM = True
except ImportError:
    HAS_TELEGRAM = False
    ApplicationBuilder = Any
    CommandHandler = Any
    MessageHandler = Any
    CallbackQueryHandler = Any
    filters = Any
    HTTPXRequest = Any

from galgame2voice.database.session import get_db
from galgame2voice.database import crud
from galgame2voice.telegram_bot.handlers import TelegramBotHandlers
from galgame2voice.telegram_bot.proxy import get_proxy_url, get_telegram_request_kwargs
from galgame2voice.utils.logger import sanitize_error_detail
from galgame2voice.utils.text_sanitize import parse_admin_ids, sanitize_bot_token

logger = logging.getLogger("galgame2voice.telegram_bot.bot")


def validate_bot_token(token: str | None) -> bool:
    """
    Validates Telegram bot token format.
    Must be non-empty, >= 10 chars, not contain 'invalid', and contain ':'.
    """
    if not token:
        return False
    t = str(token).replace(" ", "").strip()
    return len(t) >= 10 and "invalid" not in t.lower() and ":" in t


def _register_handlers(app: Any, handlers: Any, error_handler: Any | None = None) -> None:
    """Registers command, callback query, message, and error handlers to the Telegram Application."""
    # Register command handlers
    app.add_handler(CommandHandler("start", handlers.handle_start))
    app.add_handler(CommandHandler("reset", handlers.handle_reset))
    app.add_handler(CommandHandler("voice", handlers.handle_voice))
    app.add_handler(CommandHandler(["character", "char", "switch"], handlers.handle_character))
    app.add_handler(CommandHandler("model", handlers.handle_model))
    app.add_handler(CommandHandler(["nickname", "name"], handlers.handle_nickname))
    app.add_handler(CommandHandler(["console", "menu", "settings"], handlers.handle_console))
    app.add_handler(CommandHandler("help", handlers.handle_help))

    # Register callback query handler for inline keyboard buttons
    app.add_handler(CallbackQueryHandler(handlers.handle_callback_query))

    # Register message handlers
    app.add_handler(MessageHandler(filters.VOICE, handlers.handle_voice_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text_message))
    app.add_handler(MessageHandler(filters.COMMAND, handlers.handle_unknown))

    # Register global error handler for Telegram network drops and exceptions
    if error_handler and hasattr(app, "add_error_handler"):
        app.add_error_handler(error_handler)


class TelegramBotManager:
    """
    Manages Telegram Bot async application lifecycle, handlers, and background polling.
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path
        self.app: Any | None = None
        self.handlers = TelegramBotHandlers(db_path=db_path)
        self.is_running: bool = False
        self._polling_task: asyncio.Task | None = None

    async def start(self) -> bool:
        """
        Loads configuration from SQLite and starts Telegram Bot long-polling.
        Returns True if started successfully, False if disabled or unconfigured.
        """
        if self.is_running:
            logger.warning("Telegram Bot is already running.")
            return True

        async with get_db(self.db_path) as conn:
            settings = await crud.get_settings_raw(conn)

        # Telegram is an optional feature. If not enabled in settings, do not start and do not output any messages.
        if not getattr(settings, "telegram_enabled", False):
            return False

        token = sanitize_bot_token(settings.telegram_bot_token) if settings.telegram_bot_token else ""
        if not token:
            logger.warning("Telegram Bot is enabled but token is empty; skipping bot startup.")
            return False

        if not validate_bot_token(token):
            logger.warning("Invalid Telegram Bot Token configured: ****%s", token[-4:] if len(token) >= 8 else "****")
            return False

        if not HAS_TELEGRAM:
            logger.warning("python-telegram-bot is not installed; Telegram Bot service is DISABLED.")
            self.is_running = False
            return False

        admin_ids = parse_admin_ids(getattr(settings, "telegram_admin_ids", "") or "")
        admin_ids.extend(parse_admin_ids(os.getenv("TELEGRAM_ADMIN_IDS", "")))
        self.handlers.admin_ids = set(admin_ids)
        if admin_ids:
            logger.info("Telegram admin whitelist active with %d user(s).", len(admin_ids))
        else:
            logger.warning(
                "SECURITY: Telegram Bot is enabled but telegram_admin_ids is EMPTY. "
                "Fail-closed policy active: ALL users (including you) will be rejected until at least one "
                "admin user ID is configured in the console or the TELEGRAM_ADMIN_IDS env var."
            )

        proxy_url = get_proxy_url(settings)
        req_kwargs = get_telegram_request_kwargs(proxy_url)
        if proxy_url:
            logger.info("Configuring Telegram Bot with proxy: %s", proxy_url)

        request = HTTPXRequest(**req_kwargs)
        get_updates_request = HTTPXRequest(**req_kwargs)
        builder = (
            ApplicationBuilder()
            .token(token)
            .request(request)
            .get_updates_request(get_updates_request)
        )
        self.app = builder.build()

        _register_handlers(self.app, self.handlers, self._on_telegram_error)

        # Initialize and start polling
        try:
            await self.app.initialize()
            await self.app.start()
            if hasattr(self.app, "updater") and self.app.updater:
                drop_pending = os.getenv("TELEGRAM_DROP_PENDING_UPDATES", "").strip().lower() in ("1", "true", "yes")
                await self.app.updater.start_polling(drop_pending_updates=drop_pending)
            self.is_running = True
            logger.info("Telegram Bot service started successfully.")
            return True
        except Exception as exc:
            logger.error("Telegram Bot initialization failed: %s", exc)
            self.is_running = False
            try:
                if self.app and hasattr(self.app, "shutdown"):
                    await self.app.shutdown()
            except Exception as shutdown_err:
                logger.debug("Non-critical: error shutting down bot application on init failure: %s", shutdown_err)
            return False

    async def _on_telegram_error(self, update: object, context: Any) -> None:
        """Centralized error boundary for python-telegram-bot exceptions and network drops."""
        err = getattr(context, "error", None)
        err_type = type(err).__name__ if err else "UnknownError"
        safe_msg = sanitize_error_detail(err)
        if "conflict" in str(safe_msg).lower():
            logger.error("Telegram token conflict: another bot instance is polling with this token! [%s]: %s", err_type, safe_msg)
        elif any(term in str(safe_msg).lower() for term in ("timed out", "network", "connect", "timeout", "connection reset")):
            logger.debug("Telegram network/timeout transient event [%s]: %s", err_type, safe_msg)
        else:
            logger.warning("Telegram Bot error event [%s]: %s", err_type, safe_msg)

    async def stop(self) -> None:
        """Gracefully stops Telegram Bot polling and application."""
        if not self.is_running:
            return

        logger.info("Stopping Telegram Bot service...")
        self.is_running = False
        # Cancel all active voice tasks and wait for clean exit
        active_tasks = [t for t in self.handlers.user_tasks.values() if not t.done()]
        for chat_id in list(self.handlers.user_tasks.keys()):
            self.handlers.cancel_user_task(chat_id)
        if active_tasks:
            try:
                await asyncio.gather(*active_tasks, return_exceptions=True)
            except asyncio.CancelledError:
                pass
            except Exception as wait_err:
                logger.debug("Error awaiting active user tasks during bot stop: %s", wait_err)

        try:
            if self.app:
                if hasattr(self.app, "updater") and self.app.updater and getattr(self.app.updater, "running", False):
                    await self.app.updater.stop()
                if getattr(self.app, "running", False):
                    await self.app.stop()
                if hasattr(self.app, "shutdown"):
                    await self.app.shutdown()
        except Exception as exc:
            logger.warning("Error stopping Telegram Bot application: %s", exc)

        logger.info("Telegram Bot service stopped.")

    async def test_token(self, token: str, proxy_url: str | None = None) -> dict[str, Any]:
        """Tests validity of a Telegram bot token via getMe API."""
        if not validate_bot_token(token):
            return {"success": False, "message": "Invalid Telegram Bot Token format"}

        url = f"https://api.telegram.org/bot{token}/getMe"
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=10.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        username = (data.get("result") or {}).get("username", "")
                        return {"success": True, "message": f"Connected to @{username}", "info": data.get("result")}
                return {"success": False, "message": f"Telegram API error (code {resp.status_code})"}
        except Exception as exc:
            safe_err = sanitize_error_detail(exc)
            return {"success": False, "message": f"Network connection failed: {safe_err}"}


_global_bot_manager: TelegramBotManager | None = None


def get_telegram_bot_manager(db_path: str | None = None) -> TelegramBotManager:
    """Returns singleton TelegramBotManager instance."""
    global _global_bot_manager
    if _global_bot_manager is None:
        _global_bot_manager = TelegramBotManager(db_path=db_path)
    return _global_bot_manager


__all__ = [
    "validate_bot_token",
    "parse_admin_ids",
    "TelegramBotManager",
    "get_telegram_bot_manager",
    "HAS_TELEGRAM",
]
