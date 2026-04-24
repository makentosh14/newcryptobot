"""
risk_manager.py
---------------
Risk management: position sizing, safety gate, circuit breaker.

Responsibilities:
1. Size positions from account equity × risk% / (entry - SL distance).
2. Enforce rounding to symbol's qty_step and tick_size.
3. Run pre-trade safety checks (the hard gate).
4. Track daily PnL, loss streak, cooldown windows.

Every safety decision returns (allowed: bool, reason: str). Loggable.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from config import settings
from logger import get_logger
from market_data import SymbolInfo, market_data


log = get_logger(__name__)


# ---------- Trade setup dataclass ----------

@dataclass
class TradeSetup:
    """Everything needed to size and place an order."""
    symbol: str
    side: str                   # "Buy" or "Sell"
    entry: float
    stop_loss: float
    take_profit: float
    score: float
    trade_type: str = "Intraday"  # Scalp / Intraday / Swing
    qty: float = 0.0              # filled by risk_manager.size_position

    @property
    def risk_distance(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def reward_distance(self) -> float:
        return abs(self.take_profit - self.entry)

    @property
    def rr(self) -> float:
        if self.risk_distance <= 0:
            return 0.0
        return self.reward_distance / self.risk_distance

    def sl_is_valid(self) -> bool:
        """SL must be on the correct side and strictly different from entry."""
        if self.risk_distance <= 0:
            return False
        if self.side == "Buy":
            return self.stop_loss < self.entry < self.take_profit
        if self.side == "Sell":
            return self.stop_loss > self.entry > self.take_profit
        return False


# ---------- Circuit breaker ----------

@dataclass
class CircuitState:
    daily_pnl_usdt: float = 0.0
    daily_start_equity: float = 0.0
    daily_date: str = ""          # YYYY-MM-DD UTC
    loss_streak: int = 0
    cooldown_until_ts: float = 0.0
    tripped: bool = False
    trip_reason: str = ""


class CircuitBreaker:
    """Tracks daily PnL, loss streak, cooldowns. In-memory for Phase 1."""

    def __init__(self) -> None:
        self.state = CircuitState()

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def on_new_day(self, equity_now: float) -> None:
        today = self._today()
        if self.state.daily_date != today:
            log.info("Circuit breaker: new day %s, reset counters", today)
            self.state = CircuitState(
                daily_date=today,
                daily_start_equity=equity_now,
            )

    def record_trade_result(self, pnl_usdt: float) -> None:
        self.state.daily_pnl_usdt += pnl_usdt
        if pnl_usdt < 0:
            self.state.loss_streak += 1
            if self.state.loss_streak >= settings.MAX_LOSS_STREAK:
                self.state.cooldown_until_ts = (
                    time.time() + settings.LOSS_STREAK_COOLDOWN_MIN * 60
                )
                log.warning(
                    "Loss streak %d reached; cooldown %d min",
                    self.state.loss_streak, settings.LOSS_STREAK_COOLDOWN_MIN,
                )
        else:
            self.state.loss_streak = 0

    def check(self, equity_now: float) -> Tuple[bool, str]:
        """Return (ok, reason). ok=False means stop trading."""
        self.on_new_day(equity_now)

        # Cooldown
        if time.time() < self.state.cooldown_until_ts:
            remaining = int(self.state.cooldown_until_ts - time.time())
            return False, f"loss-streak cooldown {remaining}s"

        # Daily loss
        if self.state.daily_start_equity > 0:
            loss_pct = -self.state.daily_pnl_usdt / self.state.daily_start_equity * 100
            if loss_pct >= settings.MAX_DAILY_LOSS_PCT:
                self.state.tripped = True
                self.state.trip_reason = f"daily loss {loss_pct:.2f}%"
                return False, self.state.trip_reason

        return True, "ok"


# ---------- Risk manager ----------

class RiskManager:
    """Sizes positions and runs the pre-trade safety gate."""

    def __init__(self) -> None:
        self.circuit = CircuitBreaker()
        self._balance_cache: Optional[float] = None
        self._balance_ts: float = 0.0
        self._balance_ttl_sec = 60.0
        # Local book of open positions (symbol -> setup). Phase 2 will sync with exchange.
        self.open_positions: Dict[str, TradeSetup] = {}

    # ---------- Balance ----------
    def set_balance(self, balance: float) -> None:
        self._balance_cache = balance
        self._balance_ts = time.time()

    def get_balance(self) -> Optional[float]:
        if self._balance_cache is None:
            return None
        if time.time() - self._balance_ts > self._balance_ttl_sec:
            return None  # stale
        return self._balance_cache

    # ---------- Sizing ----------
    @staticmethod
    def _round_step(value: float, step: float) -> float:
        if step <= 0:
            return value
        return math.floor(value / step) * step

    def size_position(
        self, setup: TradeSetup, symbol_info: SymbolInfo, balance: float
    ) -> float:
        """Compute qty rounded down to qty_step. Returns 0 if invalid."""
        if setup.risk_distance <= 0 or balance <= 0:
            return 0.0
        risk_usdt = balance * (settings.ACCOUNT_RISK_PER_TRADE_PCT / 100.0)
        raw_qty = risk_usdt / setup.risk_distance
        qty = self._round_step(raw_qty, symbol_info.qty_step)
        if qty < symbol_info.min_order_qty:
            log.info(
                "Sizing below min: %s raw=%s step=%s min=%s",
                setup.symbol, raw_qty, symbol_info.qty_step, symbol_info.min_order_qty,
            )
            return 0.0
        return qty

    def round_price(self, price: float, symbol_info: SymbolInfo) -> float:
        if symbol_info.tick_size <= 0:
            return price
        return round(price / symbol_info.tick_size) * symbol_info.tick_size

    # ---------- Safety gate ----------
    def pre_trade_check(
        self,
        setup: TradeSetup,
        current_spread_bps: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """
        Run all safety rules. Returns (allowed, reason).
        Live-mode only rules are checked via settings.is_live_armed.
        """
        # 1. Live arming (only fails a trade if we were asked for live)
        if settings.TRADE_MODE == "live" and not settings.is_live_armed:
            return False, "live mode not fully armed"

        # 2. Valid SL
        if not setup.sl_is_valid():
            return False, "invalid stop-loss"

        # 3. Balance known
        balance = self.get_balance()
        if balance is None or balance <= 0:
            return False, "balance not confirmed"

        # 4. Symbol info known
        info = market_data.get_symbol_info(setup.symbol)
        if info is None:
            return False, "symbol info unknown"

        # 5. Min notional (qty set externally)
        if setup.qty <= 0:
            return False, "qty is zero"
        if setup.qty < info.min_order_qty:
            return False, f"qty {setup.qty} below min {info.min_order_qty}"

        # 6. Max open positions
        if len(self.open_positions) >= settings.MAX_OPEN_POSITIONS:
            return False, f"max open positions reached ({settings.MAX_OPEN_POSITIONS})"

        # 7/8. Circuit breaker (daily loss + streak cooldown)
        ok, reason = self.circuit.check(balance)
        if not ok:
            return False, f"circuit: {reason}"

        # 9. No duplicate
        if setup.symbol in self.open_positions:
            return False, "duplicate symbol already open"

        # 10. R:R minimum
        if setup.rr < settings.MIN_RR:
            return False, f"RR {setup.rr:.2f} < min {settings.MIN_RR}"

        # 11. Spread sanity (optional; skipped if not provided)
        if current_spread_bps is not None and current_spread_bps > settings.MAX_SPREAD_BPS:
            return False, f"spread {current_spread_bps:.2f}bps > max {settings.MAX_SPREAD_BPS}"

        return True, "ok"

    # ---------- Position book ----------
    def register_open(self, setup: TradeSetup) -> None:
        self.open_positions[setup.symbol] = setup
        log.info("Registered open position: %s (now %d open)",
                 setup.symbol, len(self.open_positions))

    def register_closed(self, symbol: str, pnl_usdt: float) -> None:
        self.open_positions.pop(symbol, None)
        self.circuit.record_trade_result(pnl_usdt)
        log.info("Registered closed position: %s pnl=%.4f (now %d open)",
                 symbol, pnl_usdt, len(self.open_positions))


# Module-level singleton
risk_manager = RiskManager()
