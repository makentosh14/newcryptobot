"""
websocket_manager.py
--------------------
Bybit V5 public WebSocket manager for linear kline streams.

Responsibilities:
- Connect to wss://stream.bybit.com/v5/public/linear (or testnet equivalent).
- Subscribe to kline.{interval}.{symbol} topics for given symbols/timeframes.
- Parse messages and push candles into `market_data.apply_ws_kline`.
- Auto-reconnect on disconnect with exponential backoff.
- Heartbeat (ping) every 20s.

Design notes:
- Bybit limits args per subscribe; we chunk subscriptions in batches of 10.
- If too many symbols blow WS limits, caller should trim the symbol list.
- Public stream, no auth required.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Iterable, List, Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from config import settings
from logger import get_logger
from market_data import Candle, market_data


log = get_logger(__name__)

MAINNET_URL = "wss://stream.bybit.com/v5/public/linear"
TESTNET_URL = "wss://stream-testnet.bybit.com/v5/public/linear"

SUBSCRIBE_CHUNK = 10
PING_INTERVAL_SEC = 20
MAX_BACKOFF_SEC = 60


class WebSocketManager:
    """Manages a single public WS connection with reconnect."""

    def __init__(self) -> None:
        self._symbols: List[str] = []
        self._timeframes: List[str] = []
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._connected = False

    @property
    def url(self) -> str:
        return TESTNET_URL if settings.BYBIT_TESTNET else MAINNET_URL

    @property
    def connected(self) -> bool:
        return self._connected

    def set_subscriptions(self, symbols: Iterable[str], timeframes: Iterable[str]) -> None:
        """Define which streams we want. Takes effect on next (re)connect."""
        self._symbols = list(symbols)
        self._timeframes = list(timeframes)
        log.info("WS subscriptions set: %d symbols × %d timeframes = %d streams",
                 len(self._symbols), len(self._timeframes),
                 len(self._symbols) * len(self._timeframes))

    async def start(self) -> None:
        """Start the background connect/reconnect loop."""
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_forever(), name="ws-manager")
        log.info("WebSocketManager started")

    async def stop(self) -> None:
        """Signal the loop to stop and wait."""
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
                log.warning("WS task did not stop in time; cancelled")
        self._connected = False
        log.info("WebSocketManager stopped")

    # ---------- Internal ----------
    async def _run_forever(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                await self._connect_and_handle()
                backoff = 1  # reset on clean exit
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("WS loop error: %s", e, exc_info=False)

            if self._stop.is_set():
                break

            log.warning("WS reconnecting in %ds", backoff)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                # stop was set during wait
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, MAX_BACKOFF_SEC)

    async def _connect_and_handle(self) -> None:
        log.info("Connecting to %s", self.url)
        async with websockets.connect(
            self.url,
            ping_interval=None,  # manage ping ourselves per Bybit protocol
            ping_timeout=None,
            close_timeout=5,
            max_queue=1024,
        ) as ws:
            self._connected = True
            log.info("WS connected")

            await self._subscribe_all(ws)

            ping_task = asyncio.create_task(self._ping_loop(ws), name="ws-ping")
            try:
                async for raw in ws:
                    if self._stop.is_set():
                        break
                    await self._handle_message(raw)
            except ConnectionClosed as e:
                log.warning("WS connection closed: code=%s reason=%s", e.code, e.reason)
            except WebSocketException as e:
                log.error("WS exception: %s", e)
            finally:
                ping_task.cancel()
                self._connected = False

    async def _subscribe_all(self, ws) -> None:
        """Build and send subscription requests in chunks."""
        topics: List[str] = [
            f"kline.{tf}.{sym}"
            for sym in self._symbols
            for tf in self._timeframes
        ]
        if not topics:
            log.warning("No WS topics to subscribe")
            return
        for i in range(0, len(topics), SUBSCRIBE_CHUNK):
            chunk = topics[i:i + SUBSCRIBE_CHUNK]
            msg = {"op": "subscribe", "args": chunk}
            await ws.send(json.dumps(msg))
        log.info("Subscribed to %d topics in %d chunks",
                 len(topics), (len(topics) + SUBSCRIBE_CHUNK - 1) // SUBSCRIBE_CHUNK)

    async def _ping_loop(self, ws) -> None:
        """Send Bybit-style ping every PING_INTERVAL_SEC."""
        try:
            while not self._stop.is_set():
                await asyncio.sleep(PING_INTERVAL_SEC)
                try:
                    await ws.send(json.dumps({"op": "ping", "req_id": str(int(time.time()))}))
                except Exception as e:
                    log.debug("Ping send failed: %s", e)
                    return
        except asyncio.CancelledError:
            return

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("WS non-JSON message ignored")
            return

        # Control messages
        if "op" in msg:
            op = msg.get("op")
            if op in ("subscribe", "pong", "ping", "auth"):
                if msg.get("success") is False:
                    log.warning("WS op failed: %s", msg)
                return

        topic: Optional[str] = msg.get("topic")
        data = msg.get("data")
        if not topic or not data:
            return

        if topic.startswith("kline."):
            await self._handle_kline(topic, data)

    async def _handle_kline(self, topic: str, data) -> None:
        """
        Topic format: kline.{interval}.{symbol}
        Data: list of kline dicts.
        """
        try:
            _, interval, symbol = topic.split(".", 2)
        except ValueError:
            log.debug("Bad kline topic format: %s", topic)
            return

        if not isinstance(data, list):
            return

        for k in data:
            try:
                candle = Candle(
                    start_ms=int(k["start"]),
                    open=float(k["open"]),
                    high=float(k["high"]),
                    low=float(k["low"]),
                    close=float(k["close"]),
                    volume=float(k["volume"]),
                    turnover=float(k.get("turnover", 0) or 0),
                )
                confirmed = bool(k.get("confirm", False))
                market_data.apply_ws_kline(symbol, interval, candle, confirmed)
            except (KeyError, ValueError, TypeError) as e:
                log.debug("Bad kline payload: %s", e)
                continue


# Module-level singleton
ws_manager = WebSocketManager()
