"""
Market regime detector.

Classifies the current market state for a single stock into one of four regimes
based on trend strength, momentum, and volatility:

  bull      — sustained uptrend, price above key moving averages
  bear      — sustained downtrend, price below moving averages
  sideways  — range-bound; weak trend signal
  volatile  — annualised 20-day volatility > 35%

The regime is used to tighten or relax BUY/SELL thresholds in signals.py:
  bull:     easier to buy, harder to sell  (ride the trend)
  bear:     harder to buy, easier to sell  (cut losers faster)
  volatile: very conservative on both sides (uncertainty too high)
  sideways: use config defaults
"""

import numpy as np
import pandas as pd

import config
from utils.logger import log


# Threshold deltas per regime  →  (buy_delta, sell_delta)
_REGIME_ADJUSTMENTS = {
    "bull":     (-0.04,  +0.04),
    "bear":     (+0.06,  -0.06),
    "sideways": ( 0.00,   0.00),
    "volatile": (+0.08,  -0.04),
    "unknown":  ( 0.00,   0.00),
}

# Score contribution: how bullish/bearish this regime is
_REGIME_SCORES = {
    "bull":     +0.3,
    "bear":     -0.3,
    "sideways":  0.0,
    "volatile": -0.1,
    "unknown":   0.0,
}


def detect(df: pd.DataFrame) -> dict:
    """
    Detect the current regime from an OHLCV DataFrame.

    Returns a dict:
      regime    — str: 'bull' | 'bear' | 'sideways' | 'volatile' | 'unknown'
      trend_pct — 20-day price return (%)
      vol_ann   — 20-day annualised volatility (%)
      sma_slope — 5-day slope of SMA20 (%)
      score     — float contribution [-1, +1] for use in signal weighting
    """
    close = df["Close"] if isinstance(df, pd.DataFrame) else df
    n = len(close)

    if n < 55:
        return {"regime": "unknown", "trend_pct": 0.0, "vol_ann": 0.0, "sma_slope": 0.0, "score": 0.0}

    price   = float(close.iloc[-1])
    sma20   = float(close.rolling(20).mean().iloc[-1])
    sma50   = float(close.rolling(50).mean().iloc[-1])

    # 20-day trend
    past20  = float(close.iloc[-21])
    trend_pct = (price / past20 - 1) * 100 if past20 > 0 else 0.0

    # SMA20 5-day slope
    sma20_series = close.rolling(20).mean()
    sma20_prev   = float(sma20_series.iloc[-6]) if n >= 26 else sma20
    sma_slope    = (sma20 / sma20_prev - 1) * 100 if sma20_prev > 0 else 0.0

    # 20-day annualised volatility
    vol_ann = float(close.pct_change().tail(20).std()) * np.sqrt(252) * 100

    # Classify
    if vol_ann > 35:
        regime = "volatile"
    elif trend_pct > 5 and price > sma50 and sma_slope > 0.3:
        regime = "bull"
    elif trend_pct < -5 or price < sma50 * 0.95:
        regime = "bear"
    else:
        regime = "sideways"

    return {
        "regime":    regime,
        "trend_pct": round(trend_pct, 2),
        "vol_ann":   round(vol_ann,   1),
        "sma_slope": round(sma_slope, 2),
        "score":     _REGIME_SCORES[regime],
    }


def adjusted_thresholds(regime_info: dict) -> tuple:
    """
    Return (buy_threshold, sell_threshold) adjusted for the given regime.
    Always stays within ±0.10 of the base config values.
    """
    regime = regime_info.get("regime", "unknown")
    b_delta, s_delta = _REGIME_ADJUSTMENTS.get(regime, (0.0, 0.0))
    return (
        config.BUY_THRESHOLD  + b_delta,
        config.SELL_THRESHOLD + s_delta,
    )
