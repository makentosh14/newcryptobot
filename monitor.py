"""
monitor.py
----------
Active position monitor.

Responsibilities:
- For each open paper position, periodically fetch the current price and
  check if SL or TP is hit.
- Apply break-even SL after TP1 logic and a simple ATR-based trailing stop.
- Emits close events and notifies the risk_manager.

For live positions, SL and TP are set exchange-side at entry time, so the
exchange enforces them even if the bot crashes. The monitor still runs to:
  (a) track TP1 partials (Phase 2 will send partial-close orders),
  (b) adjust SL to break-even after TP1 via set_trading_stop,
  (c) log position updates.

Phase 1 scope: paper monitoring with SL/TP hit detection.
Live monitoring modifies SL via set_trading_stop once price crosses TP1.

TODO(phase-2):
- Partial close at TP1 (e.g., 50% of size).
- Smarter trailing (ATR multiplier by trade_type).
- Time-based exits for Scalp.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from bybit_api import bybit_api
from config import settings
from logger import get_logger
from market_data import market_data
from risk_manager import TradeSetup, risk_manager
from trade_executor import Fill, trade_executor


log = get_logger(__name__)

MONITOR_INTERVAL_SEC = 2
TP1_FRACTION = 0.5  # price fraction of (entry -> TP) that counts as TP1


class PositionMonitor:
    """Polls prices and manages open positions."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        # Track which positions have reached TP1 (to move SL to BE only once)
        self._tp1_hit: set = set()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="position-monitor")
        log.info("PositionMonitor started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
        log.info("PositionMonitor stopped")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:
                log.error("Monitor tick error: %s", e, exc_info=False)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=MONITOR_INTERVAL_SEC)
                break
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        open_positions = dict(risk_manager.open_positions)  # snapshot
        if not open_positions:
            return

        is_live = trade_executor.is_live

        for symbol, setup in open_positions.items():
            price = await self._get_price(symbol)
            if price is None or price <= 0:
                continue

            # --- SL / TP check ---
            hit_sl = self._price_hit_sl(setup, price)
            hit_tp = self._price_hit_tp(setup, price)

            if hit_sl or hit_tp:
                reason = "SL" if hit_sl else "TP"
                await self._close_position(setup, price, reason, is_live)
                continue

            # --- TP1: move SL to break-even ---
            if symbol not in self._tp1_hit and self._price_hit_tp1(setup, price):
                self._tp1_hit.add(symbol)
                log.info("%s TP1 reached at %.6f — moving SL to BE (%.6f)",
                         symbol, price, setup.entry)
                setup.stop_loss = setup.entry
                if is_live:
                    await bybit_api.set_trading_stop(
                        symbol=symbol, stop_loss=setup.entry,
                    )

    async def _get_price(self, symbol: str) -> Optional[float]:
        """Prefer WS cache last close; fall back to REST ticker."""
        candles = await market_data.get_candles(symbol, "1", n=2)
        if candles:
            return candles[-1].close
        ticker = await bybit_api.get_ticker(symbol)
        if ticker:
            try:
                return float(ticker.get("lastPrice", 0) or 0) or None
            except (ValueError, TypeError):
                return None
        return None

    @staticmethod
    def _price_hit_sl(setup: TradeSetup, price: float) -> bool:
        if setup.side == "Buy":
            return price <= setup.stop_loss
        return price >= setup.stop_loss

    @staticmethod
    def _price_hit_tp(setup: TradeSetup, price: float) -> bool:
        if setup.side == "Buy":
            return price >= setup.take_profit
        return price <= setup.take_profit

    @staticmethod
    def _price_hit_tp1(setup: TradeSetup, price: float) -> bool:
        """TP1 is halfway between entry and take_profit."""
        if setup.side == "Buy":
            tp1 = setup.entry + (setup.take_profit - setup.entry) * TP1_FRACTION
            return price >= tp1
        tp1 = setup.entry - (setup.entry - setup.take_profit) * TP1_FRACTION
        return price <= tp1

    async def _close_position(
        self, setup: TradeSetup, price: float, reason: str, is_live: bool,
    ) -> None:
        log.info("Closing %s on %s hit at price=%.6f", setup.symbol, reason, price)
        pnl_usdt = 0.0
        if is_live:
            fill = await trade_executor.live.close(setup.symbol, setup.side, setup.qty)
            # Realized PnL from live will be retrieved via reconciler in Phase 2.
        else:
            fill: Optional[Fill] = trade_executor.paper.close(setup.symbol, price)
            if fill:
                pnl_usdt = fill.pnl_usdt

        self._tp1_hit.discard(setup.symbol)
        risk_manager.register_closed(setup.symbol, pnl_usdt)


# Module-level singleton
position_monitor = PositionMonitor()
