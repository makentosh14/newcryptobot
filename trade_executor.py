"""
trade_executor.py
-----------------
Order execution: paper broker + live broker, selected by TRADE_MODE.

Paper broker:
- Simulates fills at the requested entry price with configurable slippage.
- Charges taker fees on entry and exit.
- Tracks virtual equity starting at PAPER_STARTING_BALANCE_USDT.

Live broker:
- Guarded by settings.is_live_armed.
- Places market order with exchange-side SL and TP.
- Returns order id on success.

Both paths go through `TradeExecutor.execute(setup)` which runs the safety
gate, sizes the position, and places the order.

TODO(phase-2):
- Replace in-memory paper PnL tracking with DB journal.
- Add limit-order support and partial-fill handling.
- Add client order ID retries for idempotency.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from bybit_api import bybit_api
from config import settings
from logger import get_logger
from market_data import market_data
from risk_manager import RiskManager, TradeSetup, risk_manager


log = get_logger(__name__)


@dataclass
class Fill:
    symbol: str
    side: str
    qty: float
    entry_price: float
    stop_loss: float
    take_profit: float
    opened_ts: float
    order_id: str
    is_paper: bool
    fees_paid: float = 0.0
    closed: bool = False
    close_price: float = 0.0
    close_ts: float = 0.0
    pnl_usdt: float = 0.0


class PaperBroker:
    """In-memory simulated broker for safe testing."""

    def __init__(self) -> None:
        self.balance: float = settings.PAPER_STARTING_BALANCE_USDT
        self.open_fills: Dict[str, Fill] = {}
        log.info("PaperBroker initialized with balance=%.2f USDT", self.balance)

    def _apply_slippage(self, price: float, side: str) -> float:
        bps = settings.PAPER_SLIPPAGE_BPS / 10000.0
        return price * (1 + bps) if side == "Buy" else price * (1 - bps)

    def _fee(self, notional: float) -> float:
        return notional * (settings.PAPER_TAKER_FEE_BPS / 10000.0)

    def open(self, setup: TradeSetup) -> Optional[Fill]:
        entry = self._apply_slippage(setup.entry, setup.side)
        notional = entry * setup.qty
        fee = self._fee(notional)
        if fee >= self.balance:
            log.error("Paper: fee %.4f exceeds balance %.4f", fee, self.balance)
            return None
        self.balance -= fee
        fill = Fill(
            symbol=setup.symbol,
            side=setup.side,
            qty=setup.qty,
            entry_price=entry,
            stop_loss=setup.stop_loss,
            take_profit=setup.take_profit,
            opened_ts=time.time(),
            order_id=f"paper-{uuid.uuid4().hex[:10]}",
            is_paper=True,
            fees_paid=fee,
        )
        self.open_fills[setup.symbol] = fill
        log.info("Paper OPEN %s %s qty=%s entry=%.6f SL=%.6f TP=%.6f fee=%.4f",
                 fill.symbol, fill.side, fill.qty, fill.entry_price,
                 fill.stop_loss, fill.take_profit, fee)
        return fill

    def close(self, symbol: str, exit_price: float) -> Optional[Fill]:
        fill = self.open_fills.pop(symbol, None)
        if fill is None:
            return None
        exit_side = "Sell" if fill.side == "Buy" else "Buy"
        exit_price_slip = self._apply_slippage(exit_price, exit_side)
        notional = exit_price_slip * fill.qty
        fee = self._fee(notional)
        self.balance -= fee
        if fill.side == "Buy":
            pnl = (exit_price_slip - fill.entry_price) * fill.qty
        else:
            pnl = (fill.entry_price - exit_price_slip) * fill.qty
        self.balance += pnl
        fill.closed = True
        fill.close_price = exit_price_slip
        fill.close_ts = time.time()
        fill.fees_paid += fee
        fill.pnl_usdt = pnl - fee  # net of exit fee; entry fee already paid
        log.info("Paper CLOSE %s exit=%.6f pnl=%.4f balance=%.4f",
                 fill.symbol, exit_price_slip, fill.pnl_usdt, self.balance)
        return fill


class LiveBroker:
    """Real Bybit broker. Blocked unless live is armed."""

    async def open(self, setup: TradeSetup) -> Optional[Fill]:
        if not settings.is_live_armed:
            log.error("LiveBroker.open called but live is NOT armed. Refusing.")
            return None

        client_id = f"bot-{uuid.uuid4().hex[:14]}"
        result = await bybit_api.place_order(
            symbol=setup.symbol,
            side=setup.side,
            qty=setup.qty,
            order_type="Market",
            stop_loss=setup.stop_loss,
            take_profit=setup.take_profit,
            client_order_id=client_id,
        )
        if not result:
            log.error("Live order failed for %s", setup.symbol)
            return None

        order_id = result.get("result", {}).get("orderId", client_id)
        fill = Fill(
            symbol=setup.symbol,
            side=setup.side,
            qty=setup.qty,
            entry_price=setup.entry,   # actual fill price learned from position query later
            stop_loss=setup.stop_loss,
            take_profit=setup.take_profit,
            opened_ts=time.time(),
            order_id=str(order_id),
            is_paper=False,
        )
        log.info("Live OPEN accepted: %s %s qty=%s order_id=%s",
                 fill.symbol, fill.side, fill.qty, fill.order_id)
        return fill

    async def close(self, symbol: str, side: str, qty: float) -> Optional[Fill]:
        """Market close by placing opposite reduce-only order."""
        if not settings.is_live_armed:
            log.error("LiveBroker.close called but live is NOT armed. Refusing.")
            return None
        opp = "Sell" if side == "Buy" else "Buy"
        result = await bybit_api.place_order(
            symbol=symbol,
            side=opp,
            qty=qty,
            order_type="Market",
            reduce_only=True,
        )
        if not result:
            log.error("Live close failed for %s", symbol)
            return None
        log.info("Live CLOSE accepted: %s qty=%s", symbol, qty)
        # Full Fill with realized PnL is computed by reconciler in Phase 2.
        return Fill(
            symbol=symbol, side=side, qty=qty,
            entry_price=0.0, stop_loss=0.0, take_profit=0.0,
            opened_ts=0.0, order_id="", is_paper=False, closed=True,
            close_ts=time.time(),
        )


class TradeExecutor:
    """Front door for placing trades. Routes to paper or live."""

    def __init__(self) -> None:
        self.paper = PaperBroker()
        self.live = LiveBroker()
        self._rm: RiskManager = risk_manager

    @property
    def is_live(self) -> bool:
        return settings.TRADE_MODE == "live" and settings.is_live_armed

    async def execute(self, setup: TradeSetup) -> Tuple[bool, str, Optional[Fill]]:
        """
        Size, safety-check, and place the order.
        Returns (success, reason, fill_or_none).
        """
        info = market_data.get_symbol_info(setup.symbol)
        if info is None:
            return False, "symbol info missing", None

        # Round prices to tick
        setup.entry = self._rm.round_price(setup.entry, info)
        setup.stop_loss = self._rm.round_price(setup.stop_loss, info)
        setup.take_profit = self._rm.round_price(setup.take_profit, info)

        balance = self._rm.get_balance()
        if balance is None:
            return False, "balance not confirmed", None

        setup.qty = self._rm.size_position(setup, info, balance)
        if setup.qty <= 0:
            return False, "sized qty is zero", None

        allowed, reason = self._rm.pre_trade_check(setup)
        if not allowed:
            log.info("Trade rejected: %s %s -> %s", setup.symbol, setup.side, reason)
            return False, reason, None

        log.info(
            "Executing %s: %s %s qty=%s entry=%.6f SL=%.6f TP=%.6f score=%.1f (%s)",
            "LIVE" if self.is_live else "PAPER",
            setup.symbol, setup.side, setup.qty, setup.entry,
            setup.stop_loss, setup.take_profit, setup.score, setup.trade_type,
        )

        if self.is_live:
            fill = await self.live.open(setup)
        else:
            fill = self.paper.open(setup)

        if fill is None:
            return False, "broker rejected", None

        self._rm.register_open(setup)
        return True, "ok", fill


# Module-level singleton
trade_executor = TradeExecutor()
