"""
telegram_bot.py
---------------
Non-blocking Telegram alerts.

Design:
- All sends go through an in-memory asyncio queue.
- A single background worker pops from the queue and sends.
- Callers (`send(text)`) never block, never crash the bot on Telegram errors.
- If TELEGRAM_ENABLED=False or token/chat_id missing, alerts are logged locally
  and dropped silently.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from config import settings
from logger import get_logger


log = get_logger(__name__)

# Import telegram lazily inside methods so import failure doesn't break startup
try:
    from telegram import Bot
    from telegram.error import TelegramError
    _TG_AVAILABLE = True
except ImportError:
    Bot = None  # type: ignore
    TelegramError = Exception  # type: ignore
    _TG_AVAILABLE = False


class TelegramNotifier:
    """Queue-backed Telegram alert sender."""

    MAX_QUEUE = 500

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self.MAX_QUEUE)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._bot: Optional[Bot] = None

    @property
    def enabled(self) -> bool:
        return (
            settings.TELEGRAM_ENABLED
            and bool(settings.TELEGRAM_BOT_TOKEN)
            and bool(settings.TELEGRAM_CHAT_ID)
            and _TG_AVAILABLE
        )

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        if not self.enabled:
            log.info("Telegram disabled or unconfigured; alerts will be logged only")
            return
        try:
            self._bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        except Exception as e:
            log.error("Telegram Bot init failed: %s", e)
            self._bot = None
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._worker(), name="telegram-worker")
        log.info("Telegram notifier started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
        log.info("Telegram notifier stopped")

    def send(self, text: str) -> None:
        """Non-blocking enqueue. Safe to call from anywhere."""
        if not self.enabled or self._bot is None:
            log.info("[telegram-off] %s", text)
            return
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            log.warning("Telegram queue full; dropping message")

    async def _worker(self) -> None:
        assert self._bot is not None
        while not self._stop.is_set():
            try:
                text = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._bot.send_message(
                    chat_id=settings.TELEGRAM_CHAT_ID,
                    text=text[:4000],
                )
            except TelegramError as e:
                log.warning("Telegram send failed: %s", e)
            except Exception as e:
                log.error("Telegram unexpected error: %s", e, exc_info=False)


# Module-level singleton
telegram = TelegramNotifier()
