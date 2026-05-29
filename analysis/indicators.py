"""
indicators.py — single source of truth for intraday indicator math.

compute_snapshot(today_bars, prev_close, prev_avg_vol) → snapshot dict

Both the LIVE bot (screener + detail view) and the BACKTEST call this exact
function, so there is zero drift between what is tested and what trades.

The snapshot is computed from `today_bars`, which must be the 5-minute OHLCV
bars from session open up to "now" (inclusive). In live trading "now" is the
latest bar; in backtest "now" is an expanding window (bars[:i+1]).
"""

import numpy as np
import pandas as pd

EXPECTED_BARS_PER_DAY = 75   # ~9:15–15:30 in 5-min bars


def compute_snapshot(today_bars: pd.DataFrame,
                     prev_close: float | None = None,
                     prev_avg_vol: float | None = None) -> dict | None:
    """
    Compute the full intraday indicator snapshot from session-open-to-now bars.

    Parameters
    ----------
    today_bars   : DataFrame with OHLCV columns, indexed by time, for one day
                   from session open up to the current bar (inclusive).
    prev_close   : yesterday's close (for pct_chg). If None, uses today's open.
    prev_avg_vol : yesterday's full-day total volume (for vol_ratio). If None,
                   falls back to the mean bar volume of today_bars.

    Returns
    -------
    dict with keys: price, pct_chg, vol_ratio, score, vwap, above_vwap,
                    rsi, macd_hist, or_breakout, signal, bars
    or None if there are fewer than 3 bars.
    """
    if today_bars is None or len(today_bars) < 3:
        return None

    closes = today_bars["Close"]
    last_price = float(closes.iloc[-1])

    if prev_close is None or prev_close <= 0:
        prev_close = float(today_bars["Open"].iloc[0])
    pct_chg = (last_price - prev_close) / prev_close * 100

    # Volume ratio: today's volume so far vs the expected volume for this
    # fraction of the session, scaled from yesterday's full-day volume.
    today_vol = float(today_bars["Volume"].sum())
    if prev_avg_vol is None or prev_avg_vol <= 0:
        # mean bar volume × expected bars ≈ a synthetic full-day volume
        prev_avg_vol = float(today_bars["Volume"].mean()) * EXPECTED_BARS_PER_DAY
    bars_pct  = len(today_bars) / EXPECTED_BARS_PER_DAY
    vol_ratio = today_vol / max(prev_avg_vol * bars_pct, 1)

    # Momentum score (used for ranking on the screener)
    score = abs(pct_chg) * min(vol_ratio, 5)

    # VWAP
    typical_vol = ((today_bars["High"] + today_bars["Low"] + closes) / 3) \
                  * today_bars["Volume"].replace(0, np.nan)
    cum_vol  = today_bars["Volume"].replace(0, np.nan).sum()
    vwap_val = float(typical_vol.sum() / cum_vol) if cum_vol > 0 else last_price
    above_vwap = bool(last_price > vwap_val)

    # RSI — adaptive period so it works early in the session
    rsi_period = max(3, min(14, len(closes) // 2))
    delta    = closes.diff()
    avg_gain = delta.clip(lower=0).rolling(rsi_period).mean().iloc[-1]
    avg_loss = (-delta.clip(upper=0)).rolling(rsi_period).mean().iloc[-1]
    if pd.isna(avg_gain) or pd.isna(avg_loss) or avg_loss == 0:
        rsi_val = 50.0
    else:
        rsi_val = float(100 - 100 / (1 + avg_gain / avg_loss))

    # MACD histogram
    macd_line = closes.ewm(span=12, adjust=False).mean() - closes.ewm(span=26, adjust=False).mean()
    macd_hist = float((macd_line - macd_line.ewm(span=9, adjust=False).mean()).iloc[-1])

    # Opening-range breakout (first 3 bars ≈ 15 min)
    or_bars     = today_bars.iloc[:3]
    or_high     = float(or_bars["High"].max())
    or_low      = float(or_bars["Low"].min())
    or_breakout = "bullish" if last_price > or_high else "bearish" if last_price < or_low else "inside"

    # Lightweight composite signal (kept for screener display compatibility)
    sig = 0
    sig += 1 if above_vwap else -1
    sig += 2 if or_breakout == "bullish" else (-2 if or_breakout == "bearish" else 0)
    sig += 1 if rsi_val < 35 else (-1 if rsi_val > 65 else 0)
    sig += 1 if macd_hist > 0 else -1
    sig += 1 if pct_chg > 1.5 else (-1 if pct_chg < -1.5 else 0)
    signal = "BUY" if sig >= 3 else "SELL" if sig <= -3 else "HOLD"

    return {
        "price":       round(last_price, 2),
        "pct_chg":     round(pct_chg, 2),
        "vol_ratio":   round(vol_ratio, 1),
        "score":       round(score, 3),
        "vwap":        round(vwap_val, 2),
        "above_vwap":  above_vwap,
        "rsi":         round(rsi_val, 1),
        "macd_hist":   round(macd_hist, 4),
        "or_breakout": or_breakout,
        "signal":      signal,
        "bars":        len(today_bars),
    }
