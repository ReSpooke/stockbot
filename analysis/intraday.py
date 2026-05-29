"""
Intraday analysis: VWAP, opening-range breakout, 5-min RSI/MACD.

intraday_snapshot(symbol) → dict with all intraday indicators
"""

import numpy as np
import pandas as pd
import pytz
import yfinance as yf

from utils.logger import log
from analysis.indicators import compute_snapshot

_IST = pytz.timezone("Asia/Kolkata")


def _fetch_today(symbol: str) -> pd.DataFrame:
    """Return today's 5-min bars for an NSE symbol (OHLCV, IST-indexed)."""
    yf_sym = symbol + ".NS"
    df = yf.Ticker(yf_sym).history(period="5d", interval="5m", auto_adjust=True)
    if df is None or df.empty:
        return pd.DataFrame()
    df.index = df.index.tz_convert(_IST)
    today = pd.Timestamp.now(tz=_IST).date()
    return df[df.index.date == today].copy()


def vwap(df: pd.DataFrame) -> float:
    """Volume-weighted average price from OHLCV bars."""
    if df.empty:
        return float("nan")
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    vol = df["Volume"].replace(0, np.nan)
    cum_tp_vol = (typical * vol).cumsum()
    cum_vol = vol.cumsum()
    series = cum_tp_vol / cum_vol
    return round(float(series.iloc[-1]), 2)


def opening_range(df: pd.DataFrame, minutes: int = 15) -> dict:
    """First `minutes` of session → high, low, midpoint."""
    if df.empty:
        return {}
    session_start = df.index[0].replace(hour=9, minute=15, second=0, microsecond=0)
    cutoff = session_start + pd.Timedelta(minutes=minutes)
    or_bars = df[df.index <= cutoff]
    if or_bars.empty:
        return {}
    hi = float(or_bars["High"].max())
    lo = float(or_bars["Low"].min())
    return {"or_high": round(hi, 2), "or_low": round(lo, 2), "or_mid": round((hi + lo) / 2, 2)}


def _rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi_series = 100 - (100 / (1 + rs))
    val = rsi_series.iloc[-1]
    return round(float(val), 2) if not np.isnan(val) else 50.0


def _macd(series: pd.Series) -> dict:
    ema12 = series.ewm(span=12, adjust=False).mean()
    ema26 = series.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal = macd_line.ewm(span=9, adjust=False).mean()
    hist = macd_line - signal
    return {
        "macd": round(float(macd_line.iloc[-1]), 4),
        "signal": round(float(signal.iloc[-1]), 4),
        "hist": round(float(hist.iloc[-1]), 4),
    }


def intraday_snapshot(symbol: str) -> dict:
    """
    Fetch today's 5-min data and return a full intraday indicator snapshot.
    Returns empty dict if market hasn't opened or data unavailable.
    """
    try:
        yf_sym = symbol + ".NS"
        df_2d = yf.Ticker(yf_sym).history(period="5d", interval="5m", auto_adjust=True)
        if df_2d is None or df_2d.empty:
            return {}
        df_2d.index = df_2d.index.tz_convert(_IST)
        today = pd.Timestamp.now(tz=_IST).date()

        today_df = df_2d[df_2d.index.date == today].copy()
        prev_df  = df_2d[df_2d.index.date < today]

        if len(today_df) < 3:
            return {}

        prev_close = float(prev_df["Close"].iloc[-1]) if not prev_df.empty else None
        prev_day_vol = None
        if not prev_df.empty:
            prev_dates   = sorted(set(prev_df.index.date))
            prev_day_vol = float(prev_df[prev_df.index.date == prev_dates[-1]]["Volume"].sum())

        # Shared indicator math (identical to screener + backtest)
        snap = compute_snapshot(today_df, prev_close, prev_day_vol)
        if snap is None:
            return {}

        # Add detail-view-only fields (OR levels, MACD line/signal, volume)
        macd_info = _macd(today_df["Close"])
        or_info   = opening_range(today_df, minutes=15)
        snap.update({
            "symbol":      symbol,
            "prev_close":  round(prev_close, 2) if prev_close else None,
            "volume":      int(today_df["Volume"].sum()),
            "macd":        macd_info["macd"],
            "macd_signal": macd_info["signal"],
            "or_high":     or_info.get("or_high"),
            "or_low":      or_info.get("or_low"),
        })
        return snap
    except Exception as e:
        log.debug("[intraday] %s error: %s", symbol, e)
        return {}


def intraday_signal(snap: dict) -> str:
    """
    Derive a simple BUY/SELL/HOLD signal from intraday snapshot.
    Used by the auto-trading loop.
    """
    if not snap:
        return "HOLD"

    score = 0

    # VWAP direction
    if snap.get("above_vwap") is True:
        score += 1
    elif snap.get("above_vwap") is False:
        score -= 1

    # OR breakout
    ob = snap.get("or_breakout")
    if ob == "bullish":
        score += 2
    elif ob == "bearish":
        score -= 2

    # RSI
    rsi = snap.get("rsi", 50)
    if rsi < 35:
        score += 1
    elif rsi > 65:
        score -= 1

    # MACD histogram
    hist = snap.get("macd_hist", 0)
    if hist > 0:
        score += 1
    elif hist < 0:
        score -= 1

    # pct_chg momentum
    pct = snap.get("pct_chg", 0)
    if pct > 1.5:
        score += 1
    elif pct < -1.5:
        score -= 1

    if score >= 3:
        return "BUY"
    elif score <= -3:
        return "SELL"
    return "HOLD"
