"""
bybit_api.py
------------
Thin async wrapper around the pybit V5 REST client.

pybit is sync; we run its calls in a thread executor to avoid blocking
the event loop. This module centralizes:
- Client creation (testnet/mainnet toggle).
- Instrument info (precision, filters).
- Kline fetching (REST fallback to WebSocket).
- Wallet balance.
- Position and order queries (used by reconciler in Phase 2).

Every call is wrapped in try/except. Failures return None or empty list
(fail-closed) so upstream code can decide what to do.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from pybit.unified_trading import HTTP

from config import settings
from logger import get_logger


log = get_logger(__name__)


class BybitAPI:
    """Async-friendly wrapper around pybit HTTP client."""

    CATEGORY = "linear"  # USDT perpetuals

    def __init__(self) -> None:
        self._client: Optional[HTTP] = None
        self._lock = asyncio.Lock()

    # ---------- Lifecycle ----------
    async def connect(self) -> None:
        """Initialize the underlying pybit client."""
        def _build() -> HTTP:
            return HTTP(
                testnet=settings.BYBIT_TESTNET,
                api_key=settings.BYBIT_API_KEY or None,
                api_secret=settings.BYBIT_API_SECRET or None,
                recv_window=5000,
            )

        async with self._lock:
            if self._client is None:
                self._client = await asyncio.to_thread(_build)
                log.info(
                    "Bybit REST client ready (testnet=%s, auth=%s)",
                    settings.BYBIT_TESTNET,
                    bool(settings.BYBIT_API_KEY and settings.BYBIT_API_SECRET),
                )

    async def close(self) -> None:
        """Nothing persistent to close for pybit HTTP; kept for symmetry."""
        self._client = None

    def _ensure(self) -> HTTP:
        if self._client is None:
            raise RuntimeError("BybitAPI not connected. Call connect() first.")
        return self._client

    async def _run(self, fn, *args, **kwargs) -> Optional[Dict[str, Any]]:
        """Run a blocking pybit call in a thread; return result dict or None."""
        try:
            result = await asyncio.to_thread(fn, *args, **kwargs)
            if not isinstance(result, dict):
                log.warning("Unexpected Bybit response type: %s", type(result))
                return None
            if result.get("retCode") != 0:
                log.warning(
                    "Bybit API non-zero retCode: code=%s msg=%s",
                    result.get("retCode"),
                    result.get("retMsg"),
                )
                return None
            return result
        except Exception as e:  # broad on purpose; upstream must not crash
            log.error("Bybit API call failed: %s", e, exc_info=False)
            return None

    # ---------- Public endpoints ----------
    async def get_instruments(self) -> List[Dict[str, Any]]:
        """Fetch all linear USDT perpetual instruments with precision/filter info."""
        c = self._ensure()
        result = await self._run(c.get_instruments_info, category=self.CATEGORY)
        if not result:
            return []
        items = result.get("result", {}).get("list", []) or []
        # Keep only USDT-settled perpetuals that are actively trading
        filtered = [
            it for it in items
            if it.get("quoteCoin") == "USDT"
            and it.get("status", "").lower() == "trading"
            and it.get("contractType", "").lower() == "linearperpetual"
        ]
        log.info("Loaded %d linear USDT perpetual symbols", len(filtered))
        return filtered

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
    ) -> List[List[Any]]:
        """
        Fetch recent klines. Bybit returns newest-first; we reverse to oldest-first.

        Returns list of [start_ms, open, high, low, close, volume, turnover].
        """
        c = self._ensure()
        result = await self._run(
            c.get_kline,
            category=self.CATEGORY,
            symbol=symbol,
            interval=interval,
            limit=limit,
        )
        if not result:
            return []
        raw = result.get("result", {}).get("list", []) or []
        # Bybit returns strings; convert to float and reverse
        candles: List[List[Any]] = []
        for row in reversed(raw):
            try:
                candles.append([
                    int(row[0]),
                    float(row[1]),
                    float(row[2]),
                    float(row[3]),
                    float(row[4]),
                    float(row[5]),
                    float(row[6]),
                ])
            except (ValueError, IndexError, TypeError) as e:
                log.warning("Bad kline row for %s %s: %s", symbol, interval, e)
                continue
        return candles

    async def get_ticker(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetch latest ticker (last price, bid, ask) for a symbol."""
        c = self._ensure()
        result = await self._run(
            c.get_tickers, category=self.CATEGORY, symbol=symbol
        )
        if not result:
            return None
        items = result.get("result", {}).get("list", []) or []
        return items[0] if items else None

    # ---------- Private endpoints ----------
    async def get_wallet_balance(self) -> Optional[float]:
        """Return USDT equity from unified account, or None on failure."""
        c = self._ensure()
        result = await self._run(
            c.get_wallet_balance, accountType="UNIFIED", coin="USDT"
        )
        if not result:
            return None
        try:
            accounts = result.get("result", {}).get("list", []) or []
            if not accounts:
                return None
            coins = accounts[0].get("coin", []) or []
            for coin in coins:
                if coin.get("coin") == "USDT":
                    equity = coin.get("equity") or coin.get("walletBalance")
                    return float(equity) if equity else 0.0
        except (ValueError, KeyError, TypeError) as e:
            log.error("Failed to parse wallet balance: %s", e)
        return None

    async def get_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch open positions. Filter to nonzero size."""
        c = self._ensure()
        kwargs: Dict[str, Any] = {"category": self.CATEGORY, "settleCoin": "USDT"}
        if symbol:
            kwargs["symbol"] = symbol
        result = await self._run(c.get_positions, **kwargs)
        if not result:
            return []
        items = result.get("result", {}).get("list", []) or []
        return [p for p in items if float(p.get("size", 0) or 0) > 0]

    async def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "Market",
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Place an order. SL/TP are registered exchange-side when provided.

        Returns the raw result dict from Bybit on success, else None.
        """
        c = self._ensure()
        params: Dict[str, Any] = {
            "category": self.CATEGORY,
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": str(qty),
            "reduceOnly": reduce_only,
        }
        if price is not None and order_type.lower() == "limit":
            params["price"] = str(price)
        if stop_loss is not None:
            params["stopLoss"] = str(stop_loss)
        if take_profit is not None:
            params["takeProfit"] = str(take_profit)
        if client_order_id:
            params["orderLinkId"] = client_order_id

        result = await self._run(c.place_order, **params)
        if result:
            log.info(
                "Order placed: %s %s qty=%s SL=%s TP=%s",
                symbol, side, qty, stop_loss, take_profit,
            )
        return result

    async def cancel_order(
        self, symbol: str, order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Cancel a single order by id or client id."""
        c = self._ensure()
        params: Dict[str, Any] = {"category": self.CATEGORY, "symbol": symbol}
        if order_id:
            params["orderId"] = order_id
        if client_order_id:
            params["orderLinkId"] = client_order_id
        return await self._run(c.cancel_order, **params)

    async def set_trading_stop(
        self,
        symbol: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        trailing_stop: Optional[float] = None,
        position_idx: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """Modify SL/TP/trailing on an existing position (one-way mode)."""
        c = self._ensure()
        params: Dict[str, Any] = {
            "category": self.CATEGORY,
            "symbol": symbol,
            "positionIdx": position_idx,
        }
        if stop_loss is not None:
            params["stopLoss"] = str(stop_loss)
        if take_profit is not None:
            params["takeProfit"] = str(take_profit)
        if trailing_stop is not None:
            params["trailingStop"] = str(trailing_stop)
        return await self._run(c.set_trading_stop, **params)


# Module-level singleton
bybit_api = BybitAPI()
