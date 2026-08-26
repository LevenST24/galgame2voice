"""
Telegram Bot Async Lifecycle and Application Manager.
Manages python-telegram-bot Application instance, token validation, polling, and shutdown.
"""

import asyncio
import logging
from typing import Any, Dict, Optional
import httpx

try:
    from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, filters
    from telegram.request import HTTPXRequest
    HAS_TELEGRAM = True
except ImportError:
    HAS_TELEGRAM = False
    Application = Any
    ApplicationBuilder = Any
    CommandHandler = Any
    MessageHandler = Any
    filters = Any
    HTTPXRequest = Any

from galgame2voice.database.session import get_db
from galgame2voice.database import crud
from galgame2voice.telegram_bot.handlers import TelegramBotHandlers
from galgame2voice.telegram_bot.proxy import get_proxy_url, get_telegram_request_kwargs

logger = logging.getLogger("galgame2voice.telegram_bot.bot")


def validate_bot_token(token: Optional[str]) -> bool:
    """
    Validates Telegram bot token format.
    Must be non-empty, >= 10 chars, not contain 'invalid', and contain ':'.
    """
    if not token:
        return False
    t = str(token).strip()
    if len(t) < 10 or "invalid" in t.lower() or ":" not in t:
        return False
    return True


class TelegramBotManager:
    """
    Manages Telegram Bot async application lifecycle, handlers, and background polling.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path
        self.app: Optional[Any] = None
        self.handlers = TelegramBotHandlers(db_path=db_path)
        self.is_running: bool = False
        self._polling_task: Optional[asyncio.Task] = None

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

        token = settings.telegram_bot_token.strip() if settings and settings.telegram_bot_token else ""
        if not token:
            logger.info("Telegram Bot token is empty; skipping bot startup.")
            return False

        if not validate_bot_token(token):
            logger.error("Invalid Telegram Bot Token configured: %s", token[:5] + "****" if len(token) >= 5 else "****")
            raise ValueError("Invalid Telegram Bot Token")

        if not HAS_TELEGRAM:
            logger.warning("python-telegram-bot is not installed; Telegram Bot service running in mock/standby mode.")
            self.is_running = True
            return True

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

        # Register command handlers
        self.app.add_handler(CommandHandler("start", self.handlers.handle_start))
        self.app.add_handler(CommandHandler("reset", self.handlers.handle_reset))
        self.app.add_handler(CommandHandler("voice", self.handlers.handle_voice))
        self.app.add_handler(CommandHandler("model", self.handlers.handle_model))
        self.app.add_handler(CommandHandler("console", self.handlers.handle_console))
        self.app.add_handler(CommandHandler("help", self.handlers.handle_help))

        # Register message handlers
        self.app.add_handler(MessageHandler(filters.VOICE, self.handlers.handle_voice_message))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handlers.handle_text_message))
        self.app.add_handler(MessageHandler(filters.COMMAND, self.handlers.handle_unknown))

        # Initialize and start polling
        try:
            await self.app.initialize()
            await self.app.start()
            if hasattr(self.app, "updater") and self.app.updater:
                await self.app.updater.start_polling(drop_pending_updates=True)
            self.is_running = True
            logger.info("Telegram Bot service started successfully.")
            return True
        except Exception as exc:
            logger.error("Telegram Bot initialization failed: %s", exc)
            self.is_running = False
            return False

    async def stop(self) -> None:
        """Gracefully stops Telegram Bot polling and application."""
        if not self.is_running:
            return

        logger.info("Stopping Telegram Bot service...")
        self.is_running = False
        # Cancel all active voice tasks
        for chat_id in list(self.handlers.user_tasks.keys()):
            self.handlers.cancel_user_task(chat_id)

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

        logger.info("Telegram Bot service stopped cleanly.")

    async def test_token(self, token: str, proxy_url: Optional[str] = None) -> Dict[str, Any]:
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
                        username = data.get("result", {}).get("username", "")
                        return {"success": True, "message": f"Connected to @{username}", "info": data.get("result")}
                return {"success": False, "message": f"Telegram API error (code {resp.status_code})"}
        except Exception as exc:
            return {"success": False, "message": f"Network connection failed: {exc}"}


_global_bot_manager: Optional[TelegramBotManager] = None


def get_telegram_bot_manager(db_path: Optional[str] = None) -> TelegramBotManager:
    """Returns singleton TelegramBotManager instance."""
    global _global_bot_manager
    if _global_bot_manager is None:
        _global_bot_manager = TelegramBotManager(db_path=db_path)
    return _global_bot_manager


__all__ = [
    "validate_bot_token",
    "TelegramBotManager",
    "get_telegram_bot_manager",
    "HAS_TELEGRAM",
]
