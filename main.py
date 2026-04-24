"""
main.py
-------
Entry point for the crypto trading bot.

Responsibilities:
- Load config, start logger.
- Initialize Bybit REST client and fetch symbol registry.
- Start WebSocket manager for live kline data.
- Start position monitor and Telegram notifier.
- Run the scanner loop (Phase 1: logs candidate setups; does NOT auto-trade yet).
- Handle SIGINT/SIGTERM for graceful shutdown.

Phase 1 behavior:
- Scans a small subset of symbols (top volume or a fixed list).
- Scores each on the configured timeframes.
- If a high-scoring candidate emerges in paper mode, builds a TradeSetup and
  routes through trade_executor (safety-gated).
- Live mode is NOT executed unless settings.is_live_armed is True.

TODO(phase-2):
- Full scanner module with concurrent symbol scanning.
- Setup builder with proper SL/TP from ATR + structure.
- Reconciler on startup to re-adopt open exchange positions.
- SQLite trade journal.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import List

from bybit_api import bybit_api
from config import settings
from logger import get_logger
from market_data import market_data
from monitor import position_monitor
from risk_manager import TradeSetup, risk_manager
from score import Score, score_candles
from telegram_bot import telegram
from trade_executor import trade_executor
from websocket_manager import ws_manager


log = get_logger(__name__)


# Default scanning universe for Phase 1 — keep it small & liquid.
DEFAULT_UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "TONUSDT",
]


async def refresh_balance() -> None:
    """Fetch wallet balance and set it on risk_manager. Paper uses local balance."""
    if settings.TRADE_MODE == "paper":
        risk_manager.set_balance(trade_executor.paper.balance)
        return
    bal = await bybit_api.get_wallet_balance()
    if bal is not None:
        risk_manager.set_balance(bal)
        log.info("Balance refreshed: %.4f USDT", bal)
    else:
        log.warning("Could not refresh balance from Bybit")


async def balance_loop(stop: asyncio.Event) -> None:
    """Refresh balance every 30s."""
    while not stop.is_set():
        await refresh_balance()
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
            break
        except asyncio.TimeoutError:
            continue


def _build_setup_from_score(symbol: str, s: Score) -> TradeSetup | None:
    """
    Minimal Phase 1 setup builder.

    SL = entry ± 1.5 × ATR (direction-aware)
    TP = entry ± 3.0 × ATR  (RR = 2.0)

    This is intentionally simple. Phase 2 will add structure-based SL.
    """
    if s.atr_value <= 0 or s.last_close <= 0:
        return None

    sl_mult = 1.5
    tp_mult = 3.0
    entry = s.last_close

    if s.direction == "LONG":
        setup = TradeSetup(
            symbol=symbol,
            side="Buy",
            entry=entry,
            stop_loss=entry - sl_mult * s.atr_value,
            take_profit=entry + tp_mult * s.atr_value,
            score=s.total,
            trade_type="Intraday",
        )
    elif s.direction == "SHORT":
        setup = TradeSetup(
            symbol=symbol,
            side="Sell",
            entry=entry,
            stop_loss=entry + sl_mult * s.atr_value,
            take_profit=entry - tp_mult * s.atr_value,
            score=s.total,
            trade_type="Intraday",
        )
    else:
        return None

    return setup


async def scan_once(symbols: List[str]) -> None:
    """Score each symbol on its primary timeframe; log and optionally execute."""
    # Primary TF for signal: use the middle one from configured list (e.g. 15m)
    tfs = settings.timeframes_list
    primary_tf = tfs[len(tfs) // 2] if tfs else "15"

    for symbol in symbols:
        candles = await market_data.get_candles(symbol, primary_tf, n=200)
        if len(candles) < 60:
            continue

        s = score_candles(candles)
        if not s.is_actionable(settings.MIN_SCORE_TO_TRADE):
            log.debug("%s score=%.1f dir=%s below threshold",
                      symbol, s.total, s.direction)
            continue

        log.info(
            "CANDIDATE %s tf=%s dir=%s score=%.1f (trend=%.1f mom=%.1f vol=%.1f vola=%.1f struct=%.1f)",
            symbol, primary_tf, s.direction, s.total,
            s.trend, s.momentum, s.volume, s.volatility, s.structure,
        )
        log.info("  reasons: %s", "; ".join(s.reasons[:5]))

        setup = _build_setup_from_score(symbol, s)
        if setup is None:
            continue

        ok, reason, fill = await trade_executor.execute(setup)
        if ok and fill:
            telegram.send(
                f"🟢 {setup.side} {setup.symbol}\n"
                f"score={s.total:.1f} type={setup.trade_type}\n"
                f"entry={setup.entry:.6f}\n"
                f"SL={setup.stop_loss:.6f}\n"
                f"TP={setup.take_profit:.6f}\n"
                f"mode={'LIVE' if trade_executor.is_live else 'PAPER'}"
            )
        else:
            log.info("Not executed: %s", reason)


async def scanner_loop(stop: asyncio.Event) -> None:
    """Repeatedly scan the universe at SCAN_INTERVAL_SEC."""
    while not stop.is_set():
        try:
            universe = [s for s in DEFAULT_UNIVERSE
                        if market_data.get_symbol_info(s) is not None]
            if not universe:
                log.warning("Scanner: no known symbols in registry yet")
            else:
                await scan_once(universe)
        except Exception as e:
            log.error("Scanner error: %s", e, exc_info=False)
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.SCAN_INTERVAL_SEC)
            break
        except asyncio.TimeoutError:
            continue


async def symbol_refresh_loop(stop: asyncio.Event) -> None:
    """Refresh symbol registry every SYMBOL_REFRESH_HOURS."""
    interval_sec = max(3600, settings.SYMBOL_REFRESH_HOURS * 3600)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_sec)
            break
        except asyncio.TimeoutError:
            try:
                await market_data.refresh_symbols()
            except Exception as e:
                log.error("Symbol refresh error: %s", e, exc_info=False)


async def heartbeat_loop(stop: asyncio.Event) -> None:
    """Log bot health every 60s."""
    while not stop.is_set():
        bal = risk_manager.get_balance()
        bal_str = f"{bal:.4f}" if bal is not None else "N/A"
        log.info(
            "HEARTBEAT ws_connected=%s open_positions=%d mode=%s balance=%s",
            ws_manager.connected,
            len(risk_manager.open_positions),
            settings.TRADE_MODE,
            bal_str,
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
            break
        except asyncio.TimeoutError:
            continue


async def run() -> None:
    log.info("=" * 60)
    log.info("Bybit Crypto Bot starting")
    log.info(settings.summary())
    log.info("=" * 60)

    if settings.TRADE_MODE == "live" and not settings.is_live_armed:
        log.warning(
            "TRADE_MODE=live but safety flags not both True — orders will be BLOCKED. "
            "Set ENABLE_LIVE_TRADING=True and I_ACCEPT_LIVE_RISK=True to arm."
        )

    stop = asyncio.Event()

    # --- Graceful shutdown ---
    loop = asyncio.get_running_loop()

    def _signal_handler():
        log.info("Shutdown signal received")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows fallback
            signal.signal(sig, lambda *a: _signal_handler())

    # --- Startup sequence ---
    try:
        await bybit_api.connect()
    except Exception as e:
        log.critical("Bybit connect failed: %s", e)
        return

    count = await market_data.refresh_symbols()
    if count == 0:
        log.critical("Symbol registry empty; aborting.")
        return

    # Warm initial candles for the default universe on primary tf
    tfs = settings.timeframes_list
    primary_tf = tfs[len(tfs) // 2] if tfs else "15"
    for sym in DEFAULT_UNIVERSE:
        if market_data.get_symbol_info(sym):
            await market_data.get_candles(sym, primary_tf, n=200)

    # Start telegram, then announce startup
    await telegram.start()
    telegram.send(f"🤖 Bot started — {settings.summary()}")

    # Prime risk_manager balance
    await refresh_balance()

    # Start WS (kline streams for universe on primary tf only for Phase 1)
    ws_manager.set_subscriptions(
        symbols=[s for s in DEFAULT_UNIVERSE if market_data.get_symbol_info(s)],
        timeframes=[primary_tf],
    )
    await ws_manager.start()

    # Start position monitor
    await position_monitor.start()

    # Background loops
    tasks = [
        asyncio.create_task(scanner_loop(stop), name="scanner"),
        asyncio.create_task(heartbeat_loop(stop), name="heartbeat"),
        asyncio.create_task(balance_loop(stop), name="balance"),
        asyncio.create_task(symbol_refresh_loop(stop), name="symbol-refresh"),
    ]

    await stop.wait()

    # --- Shutdown sequence ---
    log.info("Shutting down…")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    await position_monitor.stop()
    await ws_manager.stop()
    await telegram.stop()
    await bybit_api.close()
    log.info("Bye.")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        # Already handled via signal, but just in case
        pass
    except Exception as e:
        log.critical("Fatal error: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
