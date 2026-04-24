"""
market_data.py
--------------
Unified market data layer.

Responsibilities:
- Maintain the symbol registry (precision, filters) refreshed periodically.
- Keep a rolling in-memory OHLCV cache per (symbol, timeframe).
- Provide a single `get_candles()` that reads cache first, falls back to REST.
- Accept candle updates from the WebSocket manager.

Cache size per (symbol, tf) is capped so memory stays bounded.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple

from bybit_api import bybit_api
from config import settings
from logger import get_logger


log = get_logger(__name__)

MAX_CACHE_PER_SERIES = 500  # candles per symbol/timeframe


@dataclass(frozen=True)
class SymbolInfo:
    """Static info for a symbol needed to place valid orders."""
    symbol: str
    tick_size: float
    qty_step: float
    min_order_qty: float
    max_leverage: float


@dataclass
class Candle:
    """OHLCV candle (float-typed)."""
    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float

    @classmethod
    def from_list(cls, row: List[Any]) -> "Candle":
        return cls(
            start_ms=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            turnover=float(row[6]) if len(row) > 6 else 0.0,
        )


class MarketData:
    """Holds symbols + rolling candle caches."""

    def __init__(self) -> None:
        self._symbols: Dict[str, SymbolInfo] = {}
        self._candles: Dict[Tuple[str, str], Deque[Candle]] = {}
        self._last_refresh_ts: float = 0.0
        self._lock = asyncio.Lock()

    # ---------- Symbols ----------
    async def refresh_symbols(self) -> int:
        """Fetch instrument info; populate symbol registry. Returns count."""
        raw = await bybit_api.get_instruments()
        if not raw:
            log.warning("Symbol refresh returned empty; keeping old registry (%d)",
                        len(self._symbols))
            return len(self._symbols)

        new_registry: Dict[str, SymbolInfo] = {}
        for it in raw:
            try:
                lot = it.get("lotSizeFilter", {}) or {}
                price = it.get("priceFilter", {}) or {}
                lev = it.get("leverageFilter", {}) or {}
                info = SymbolInfo(
                    symbol=it["symbol"],
                    tick_size=float(price.get("tickSize", 0) or 0),
                    qty_step=float(lot.get("qtyStep", 0) or 0),
                    min_order_qty=float(lot.get("minOrderQty", 0) or 0),
                    max_leverage=float(lev.get("maxLeverage", 0) or 0),
                )
                if info.tick_size > 0 and info.qty_step > 0:
                    new_registry[info.symbol] = info
            except (KeyError, ValueError, TypeError) as e:
                log.warning("Skipping symbol due to parse error: %s", e)
                continue

        async with self._lock:
            self._symbols = new_registry
            self._last_refresh_ts = time.time()

        log.info("Symbol registry refreshed: %d symbols", len(new_registry))
        return len(new_registry)

    def get_symbol_info(self, symbol: str) -> Optional[SymbolInfo]:
        return self._symbols.get(symbol)

    def all_symbols(self) -> List[str]:
        return list(self._symbols.keys())

    # ---------- Candles ----------
    def _key(self, symbol: str, tf: str) -> Tuple[str, str]:
        return (symbol, str(tf))

    async def get_candles(
        self, symbol: str, tf: str, n: int = 200, force_refresh: bool = False
    ) -> List[Candle]:
        """
        Return up to `n` most recent candles (oldest-first).
        Uses cache if warm enough; otherwise REST.
        """
        key = self._key(symbol, tf)
        cached = self._candles.get(key)

        need_refresh = (
            force_refresh
            or cached is None
            or len(cached) < n
        )

        if need_refresh:
            rest = await bybit_api.get_klines(symbol, tf, limit=max(n, 200))
            if rest:
                dq: Deque[Candle] = deque(
                    (Candle.from_list(r) for r in rest),
                    maxlen=MAX_CACHE_PER_SERIES,
                )
                self._candles[key] = dq
                cached = dq
            else:
                log.debug("REST kline fetch empty for %s %s", symbol, tf)

        if not cached:
            return []
        return list(cached)[-n:]

    def apply_ws_kline(
        self, symbol: str, tf: str, candle: Candle, confirmed: bool
    ) -> None:
        """
        Merge a WebSocket kline tick into the cache.

        Bybit pushes both live (unconfirmed) and closed (confirmed) candles
        on the same stream. We upsert by start_ms:
        - If start_ms matches the last cached candle, replace it.
        - Else if start_ms is newer, append.
        - Else (stale), ignore.
        """
        key = self._key(symbol, tf)
        dq = self._candles.get(key)
        if dq is None:
            dq = deque(maxlen=MAX_CACHE_PER_SERIES)
            self._candles[key] = dq

        if dq and dq[-1].start_ms == candle.start_ms:
            dq[-1] = candle
        elif not dq or candle.start_ms > dq[-1].start_ms:
            dq.append(candle)
        else:
            return  # stale

        if confirmed:
            log.debug("Confirmed candle %s %s close=%.6f",
                      symbol, tf, candle.close)


# Module-level singleton
market_data = MarketData()
