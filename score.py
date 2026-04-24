"""
score.py
--------
Signal scoring engine.

Given a list of Candles, returns a Score with:
- direction: "LONG", "SHORT", or "NONE"
- total: 0-100 overall
- sub-scores: trend, momentum, volume, volatility, structure
- reasons: human-readable list explaining each contribution

Philosophy:
- Scoring is transparent. No black box. Every point earned has a reason.
- Default thresholds are conservative. Tune only after paper-trade data.
- Returns NONE if data is insufficient rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import numpy as np

from indicators import (
    atr, bollinger, candles_to_arrays, ema, last, macd, rsi, slope, volume_ma,
)
from logger import get_logger
from market_data import Candle


log = get_logger(__name__)


@dataclass
class Score:
    direction: str = "NONE"          # LONG / SHORT / NONE
    total: float = 0.0               # 0..100
    trend: float = 0.0               # 0..25
    momentum: float = 0.0            # 0..25
    volume: float = 0.0              # 0..20
    volatility: float = 0.0          # 0..15
    structure: float = 0.0           # 0..15
    reasons: List[str] = field(default_factory=list)
    atr_value: float = 0.0
    last_close: float = 0.0

    def is_actionable(self, threshold: float) -> bool:
        return self.direction in ("LONG", "SHORT") and self.total >= threshold


def score_candles(candles: List[Candle]) -> Score:
    """
    Score a single-timeframe series. Caller can combine multiple timeframes.

    Needs at least ~60 candles for meaningful output.
    """
    if len(candles) < 60:
        return Score(reasons=["insufficient data (<60 candles)"])

    arrs = candles_to_arrays(candles)
    close = arrs["close"]
    high = arrs["high"]
    low = arrs["low"]
    vol = arrs["volume"]

    ema20 = ema(close, 20)
    ema50 = ema(close, 50)
    rsi14 = rsi(close, 14)
    macd_line, macd_sig, macd_hist = macd(close)
    atr14 = atr(high, low, close, 14)
    bb_up, bb_mid, bb_lo = bollinger(close, 20, 2.0)
    vol_ma20 = volume_ma(vol, 20)

    last_close = last(close)
    last_ema20 = last(ema20)
    last_ema50 = last(ema50)
    last_rsi = last(rsi14)
    last_hist = last(macd_hist)
    last_atr = last(atr14)
    last_vol = last(vol)
    last_vol_ma = last(vol_ma20)
    last_bb_up = last(bb_up)
    last_bb_lo = last(bb_lo)

    # Guard against NaN outputs
    scalars = [last_close, last_ema20, last_ema50, last_rsi,
               last_hist, last_atr, last_vol, last_vol_ma]
    if any(np.isnan(x) for x in scalars):
        return Score(reasons=["indicator warm-up incomplete"])

    reasons: List[str] = []

    # ---------- Direction bias from EMAs ----------
    long_bias = last_close > last_ema20 > last_ema50
    short_bias = last_close < last_ema20 < last_ema50

    if not long_bias and not short_bias:
        return Score(
            last_close=last_close,
            atr_value=last_atr,
            reasons=["no clear EMA alignment"],
        )

    direction = "LONG" if long_bias else "SHORT"

    # ---------- Trend (0..25) ----------
    trend_score = 0.0
    # EMA20 slope
    slope20 = slope(ema20, 5)
    if direction == "LONG" and slope20 > 1e-9:
        trend_score += 10
        reasons.append(f"EMA20 rising (slope={slope20:.4f})")
    elif direction == "SHORT" and slope20 < 1e-9:
        trend_score += 10
        reasons.append(f"EMA20 falling (slope={slope20:.4f})")

    # EMA separation relative to ATR
    sep = abs(last_ema20 - last_ema50)
    if last_atr > 0 and sep / last_atr > 0.5:
        trend_score += 8
        reasons.append(f"EMA separation {sep / last_atr:.2f}×ATR")

    # Close distance above/below EMA50
    dist = (last_close - last_ema50) / last_ema50 if last_ema50 else 0
    if direction == "LONG" and dist > 0.002:
        trend_score += 7
        reasons.append(f"price {dist*100:.2f}% above EMA50")
    elif direction == "SHORT" and dist < -0.002:
        trend_score += 7
        reasons.append(f"price {abs(dist)*100:.2f}% below EMA50")

    trend_score = min(trend_score, 25)

    # ---------- Momentum (0..25) ----------
    mom_score = 0.0
    if direction == "LONG":
        if 50 < last_rsi < 70:
            mom_score += 10
            reasons.append(f"RSI healthy ({last_rsi:.1f})")
        elif last_rsi >= 70:
            mom_score += 3  # overbought penalty
            reasons.append(f"RSI overbought ({last_rsi:.1f})")
        if last_hist > 0:
            mom_score += 10
            reasons.append(f"MACD hist positive ({last_hist:.4f})")
        # MACD histogram increasing
        if len(macd_hist) >= 3 and macd_hist[-1] > macd_hist[-2] > macd_hist[-3]:
            mom_score += 5
            reasons.append("MACD hist rising 3 bars")
    else:  # SHORT
        if 30 < last_rsi < 50:
            mom_score += 10
            reasons.append(f"RSI healthy bearish ({last_rsi:.1f})")
        elif last_rsi <= 30:
            mom_score += 3
            reasons.append(f"RSI oversold ({last_rsi:.1f})")
        if last_hist < 0:
            mom_score += 10
            reasons.append(f"MACD hist negative ({last_hist:.4f})")
        if len(macd_hist) >= 3 and macd_hist[-1] < macd_hist[-2] < macd_hist[-3]:
            mom_score += 5
            reasons.append("MACD hist falling 3 bars")

    mom_score = min(mom_score, 25)

    # ---------- Volume (0..20) ----------
    vol_score = 0.0
    if last_vol_ma > 0:
        vol_ratio = last_vol / last_vol_ma
        if vol_ratio > 1.5:
            vol_score = 20
            reasons.append(f"volume surge {vol_ratio:.2f}×")
        elif vol_ratio > 1.2:
            vol_score = 14
            reasons.append(f"volume elevated {vol_ratio:.2f}×")
        elif vol_ratio > 0.9:
            vol_score = 8
            reasons.append(f"volume normal {vol_ratio:.2f}×")
        else:
            vol_score = 3
            reasons.append(f"volume low {vol_ratio:.2f}×")

    # ---------- Volatility (0..15) ----------
    # Prefer medium ATR/price ratio; too low = dead, too high = chaos.
    vola_score = 0.0
    if last_close > 0:
        atr_pct = (last_atr / last_close) * 100
        if 0.2 <= atr_pct <= 2.0:
            vola_score = 15
            reasons.append(f"volatility healthy (ATR={atr_pct:.2f}%)")
        elif 0.1 <= atr_pct < 0.2 or 2.0 < atr_pct <= 3.5:
            vola_score = 8
            reasons.append(f"volatility marginal (ATR={atr_pct:.2f}%)")
        else:
            vola_score = 2
            reasons.append(f"volatility poor (ATR={atr_pct:.2f}%)")

    # ---------- Structure (0..15) ----------
    # Simple: higher highs/higher lows for LONG, lower highs/lower lows for SHORT.
    struct_score = 0.0
    if len(high) >= 20:
        recent_high = high[-10:]
        recent_low = low[-10:]
        prev_high = high[-20:-10]
        prev_low = low[-20:-10]
        if direction == "LONG":
            if np.max(recent_high) > np.max(prev_high):
                struct_score += 8
                reasons.append("higher high confirmed")
            if np.min(recent_low) > np.min(prev_low):
                struct_score += 7
                reasons.append("higher low confirmed")
        else:
            if np.min(recent_low) < np.min(prev_low):
                struct_score += 8
                reasons.append("lower low confirmed")
            if np.max(recent_high) < np.max(prev_high):
                struct_score += 7
                reasons.append("lower high confirmed")
    struct_score = min(struct_score, 15)

    total = trend_score + mom_score + vol_score + vola_score + struct_score

    return Score(
        direction=direction,
        total=round(total, 2),
        trend=round(trend_score, 2),
        momentum=round(mom_score, 2),
        volume=round(vol_score, 2),
        volatility=round(vola_score, 2),
        structure=round(struct_score, 2),
        reasons=reasons,
        atr_value=last_atr,
        last_close=last_close,
    )
