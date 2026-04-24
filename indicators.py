"""
indicators.py
-------------
Pure-function technical indicators on numpy arrays.

No side effects, no state. Each function takes numpy arrays and returns
numpy arrays (or scalars). Easy to unit-test.

Inputs are expected to be 1-D numpy arrays of floats, ordered oldest-first.
Functions return arrays of the same length, with NaN for warm-up periods.
"""

from __future__ import annotations

from typing import List

import numpy as np

from market_data import Candle


# ---------- Conversion helpers ----------

def candles_to_arrays(candles: List[Candle]) -> dict:
    """Convert list of Candle to dict of numpy arrays."""
    if not candles:
        empty = np.array([], dtype=float)
        return {"open": empty, "high": empty, "low": empty,
                "close": empty, "volume": empty}
    return {
        "open":   np.array([c.open for c in candles],   dtype=float),
        "high":   np.array([c.high for c in candles],   dtype=float),
        "low":    np.array([c.low for c in candles],    dtype=float),
        "close":  np.array([c.close for c in candles],  dtype=float),
        "volume": np.array([c.volume for c in candles], dtype=float),
    }


# ---------- Core indicators ----------

def sma(values: np.ndarray, period: int) -> np.ndarray:
    """Simple moving average."""
    if period <= 0 or len(values) < period:
        return np.full_like(values, np.nan, dtype=float)
    out = np.full_like(values, np.nan, dtype=float)
    cumsum = np.cumsum(np.insert(values, 0, 0.0))
    out[period - 1:] = (cumsum[period:] - cumsum[:-period]) / period
    return out


def ema(values: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average, NaN until period-1 values accumulated."""
    if period <= 0 or len(values) == 0:
        return np.full_like(values, np.nan, dtype=float)
    out = np.full_like(values, np.nan, dtype=float)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1.0)
    # Seed with SMA of first `period` values
    out[period - 1] = np.mean(values[:period])
    for i in range(period, len(values)):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder's RSI."""
    n = len(close)
    out = np.full(n, np.nan, dtype=float)
    if n <= period:
        return out
    delta = np.diff(close)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else float("inf")
        out[i + 1] = 100.0 - (100.0 / (1.0 + rs)) if avg_loss > 0 else 100.0
    return out


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder's Average True Range."""
    n = len(close)
    out = np.full(n, np.nan, dtype=float)
    if n <= period:
        return out
    tr = np.zeros(n, dtype=float)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    out[period] = np.mean(tr[1:period + 1])
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def macd(
    close: np.ndarray,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
):
    """Return (macd_line, signal_line, hist)."""
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    line = ema_fast - ema_slow
    sig = ema(np.nan_to_num(line, nan=0.0), signal)
    # Re-mask sig where line is NaN
    sig[np.isnan(line)] = np.nan
    hist = line - sig
    return line, sig, hist


def bollinger(close: np.ndarray, period: int = 20, mult: float = 2.0):
    """Return (upper, middle, lower)."""
    mid = sma(close, period)
    n = len(close)
    std = np.full(n, np.nan, dtype=float)
    if n >= period:
        for i in range(period - 1, n):
            std[i] = np.std(close[i - period + 1: i + 1], ddof=0)
    upper = mid + mult * std
    lower = mid - mult * std
    return upper, mid, lower


def volume_ma(volume: np.ndarray, period: int = 20) -> np.ndarray:
    """Simple MA on volume."""
    return sma(volume, period)


# ---------- Scalar helpers ----------

def last(arr: np.ndarray) -> float:
    """Return last non-NaN value or NaN."""
    if len(arr) == 0:
        return float("nan")
    v = arr[-1]
    return float(v) if not np.isnan(v) else float("nan")


def slope(values: np.ndarray, lookback: int = 5) -> float:
    """
    Linear slope of last `lookback` values per index.
    Returns NaN if not enough data.
    """
    arr = values[~np.isnan(values)]
    if len(arr) < lookback:
        return float("nan")
    y = arr[-lookback:]
    x = np.arange(lookback, dtype=float)
    # polyfit degree 1
    m, _ = np.polyfit(x, y, 1)
    return float(m)
