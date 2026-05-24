"""
Technical indicator calculations (pure pandas/numpy — no TA-Lib required).

Indicators implemented:
  - RSI  (Relative Strength Index)
  - MACD (Moving Average Convergence/Divergence)
  - Bollinger Bands
  - SMA  (Simple Moving Average)
  - EMA  (Exponential Moving Average)
  - Volume Z-score
"""

import numpy as np
import pandas as pd

import config


# ---------------------------------------------------------------------------
# Core indicators
# ---------------------------------------------------------------------------

def compute_rsi(close: pd.Series, period: int = config.RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.rename("RSI")


def compute_macd(
    close: pd.Series,
    fast: int   = config.EMA_FAST,
    slow: int   = config.EMA_SLOW,
    signal: int = config.MACD_SIGNAL_PERIOD,
) -> pd.DataFrame:
    ema_fast   = close.ewm(span=fast,   adjust=False).mean()
    ema_slow   = close.ewm(span=slow,   adjust=False).mean()
    macd_line  = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram  = macd_line - signal_line
    return pd.DataFrame({
        "MACD":      macd_line,
        "MACD_sig":  signal_line,
        "MACD_hist": histogram,
    })


def compute_bollinger(
    close: pd.Series,
    period: int  = config.BB_PERIOD,
    n_std: float = config.BB_STD,
) -> pd.DataFrame:
    mid   = close.rolling(period).mean()
    std   = close.rolling(period).std()
    upper = mid + n_std * std
    lower = mid - n_std * std
    pct_b = (close - lower) / (upper - lower).replace(0, np.nan)
    return pd.DataFrame({
        "BB_mid":   mid,
        "BB_upper": upper,
        "BB_lower": lower,
        "BB_pct":   pct_b,
    })


def compute_sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(period).mean().rename(f"SMA{period}")


def compute_ema(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean().rename(f"EMA{period}")


def compute_volume_zscore(
    volume: pd.Series,
    period: int = config.VOLUME_AVG_PERIOD,
) -> pd.Series:
    roll_mean = volume.rolling(period).mean()
    roll_std  = volume.rolling(period).std().replace(0, np.nan)
    return ((volume - roll_mean) / roll_std).rename("VOL_Z")


# ---------------------------------------------------------------------------
# All-in-one enrichment
# ---------------------------------------------------------------------------

def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add all indicator columns to a copy of *df*.
    Input must have columns: Open, High, Low, Close, Volume.
    """
    df = df.copy()
    close  = df["Close"]
    volume = df["Volume"]

    df["RSI"]          = compute_rsi(close)
    macd_df            = compute_macd(close)
    df["MACD"]         = macd_df["MACD"]
    df["MACD_sig"]     = macd_df["MACD_sig"]
    df["MACD_hist"]    = macd_df["MACD_hist"]
    bb_df              = compute_bollinger(close)
    df["BB_upper"]     = bb_df["BB_upper"]
    df["BB_lower"]     = bb_df["BB_lower"]
    df["BB_mid"]       = bb_df["BB_mid"]
    df["BB_pct"]       = bb_df["BB_pct"]
    df[f"SMA{config.SMA_SHORT}"] = compute_sma(close, config.SMA_SHORT)
    df[f"SMA{config.SMA_LONG}"]  = compute_sma(close, config.SMA_LONG)
    df["VOL_Z"]        = compute_volume_zscore(volume)

    return df


# ---------------------------------------------------------------------------
# Latest-bar snapshot
# ---------------------------------------------------------------------------

def latest_snapshot(df: pd.DataFrame) -> dict:
    """Return a dict of the most-recent indicator values (NaN → None)."""
    enriched = enrich(df)
    if enriched.empty:
        return {}
    last = enriched.iloc[-1]
    prev = enriched.iloc[-2] if len(enriched) >= 2 else last

    def _val(col: str):
        v = last.get(col)
        return None if (v is None or (isinstance(v, float) and np.isnan(v))) else float(v)

    sma_s = f"SMA{config.SMA_SHORT}"
    sma_l = f"SMA{config.SMA_LONG}"

    return {
        "close":         _val("Close"),
        "rsi":           _val("RSI"),
        "macd":          _val("MACD"),
        "macd_signal":   _val("MACD_sig"),
        "macd_hist":     _val("MACD_hist"),
        "macd_hist_prev":float(prev.get("MACD_hist") or 0),
        "bb_upper":      _val("BB_upper"),
        "bb_lower":      _val("BB_lower"),
        "bb_pct":        _val("BB_pct"),
        "sma_short":     _val(sma_s),
        "sma_long":      _val(sma_l),
        "vol_z":         _val("VOL_Z"),
        # crossover flags
        "sma_short_prev":float(prev.get(sma_s) or 0),
        "sma_long_prev": float(prev.get(sma_l) or 0),
        "macd_prev":     float(prev.get("MACD") or 0),
        "macd_sig_prev": float(prev.get("MACD_sig") or 0),
    }
