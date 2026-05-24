"""Fetch OHLCV price data via yfinance, with multi-market symbol support."""

import time
from datetime import datetime
from typing import Optional

import pandas as pd
import yfinance as yf

import config
from utils.logger import log

_MAX_RETRIES = 3
_RETRY_DELAY = 2


def _yf_symbol(symbol: str, market: str = None) -> str:
    """Return yfinance-qualified symbol (adds .NS for NSE, uses as-is for US)."""
    if "." in symbol or "=" in symbol:
        return symbol
    if market is None:
        market = config.SYMBOL_MARKET.get(symbol, config.MARKET)
    if market == "NSE":
        return symbol + ".NS"
    return symbol


def fetch_ohlcv(
    symbol: str,
    period: str   = config.DATA_PERIOD,
    interval: str = config.DATA_INTERVAL,
) -> Optional[pd.DataFrame]:
    yf_sym = _yf_symbol(symbol)
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            df = yf.Ticker(yf_sym).history(
                period=period, interval=interval, auto_adjust=True
            )
            if df.empty:
                log.warning("No OHLCV data for %s (%s)", symbol, yf_sym)
                return None
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
            log.debug("Fetched %d rows for %s", len(df), symbol)
            return df
        except Exception as exc:
            log.warning("OHLCV attempt %d/%d failed for %s: %s", attempt, _MAX_RETRIES, symbol, exc)
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY)
    return None


def get_current_price(symbol: str) -> Optional[float]:
    yf_sym = _yf_symbol(symbol)
    try:
        info  = yf.Ticker(yf_sym).fast_info
        price = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
        if price:
            return float(price)
        df = yf.Ticker(yf_sym).history(period="2d", interval="1d", auto_adjust=True)
        if not df.empty:
            return float(df["Close"].iloc[-1])
    except Exception as exc:
        log.warning("Price fetch failed for %s: %s", symbol, exc)
    return None


def get_batch_prices(symbols: list) -> dict:
    """Fetch latest prices for all symbols in one shot."""
    yf_syms = [_yf_symbol(s) for s in symbols]
    sym_map  = {_yf_symbol(s): s for s in symbols}   # yf_sym → base sym
    prices: dict = {}
    try:
        raw = yf.download(
            " ".join(yf_syms),
            period="2d",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        if raw.empty:
            raise ValueError("empty download")
        close = raw["Close"] if "Close" in raw else raw
        if hasattr(close, "columns"):
            for yf_sym in close.columns:
                base = sym_map.get(yf_sym, yf_sym.replace(".NS", "").replace(".BO", ""))
                val  = close[yf_sym].dropna()
                if not val.empty:
                    prices[base] = float(val.iloc[-1])
        else:
            # single symbol
            if symbols:
                prices[symbols[0]] = float(close.dropna().iloc[-1])
    except Exception as exc:
        log.warning("Batch price download failed: %s — falling back", exc)
        for sym in symbols:
            p = get_current_price(sym)
            if p:
                prices[sym] = p
    return prices


def is_market_open(market: str = None) -> bool:
    """Return True if the given market is currently open (Mon-Fri, local trading hours)."""
    if market is None:
        market = config.MARKET
    hours = config.MARKET_HOURS.get(market)
    if not hours:
        return False
    try:
        import pytz
        tz  = pytz.timezone(hours["tz"])
        now = datetime.now(tz)
        if now.weekday() >= 5:
            return False
        open_t  = now.replace(hour=hours["open"][0],  minute=hours["open"][1],  second=0, microsecond=0)
        close_t = now.replace(hour=hours["close"][0], minute=hours["close"][1], second=0, microsecond=0)
        return open_t <= now <= close_t
    except Exception:
        return False


def get_all_market_status() -> dict:
    """Return open/closed status and local time for every exchange in MARKET_HOURS."""
    try:
        import pytz
    except ImportError:
        return {}
    result = {}
    for market, hours in config.MARKET_HOURS.items():
        try:
            tz  = pytz.timezone(hours["tz"])
            now = datetime.now(tz)
            open_t  = now.replace(hour=hours["open"][0],  minute=hours["open"][1],  second=0, microsecond=0)
            close_t = now.replace(hour=hours["close"][0], minute=hours["close"][1], second=0, microsecond=0)
            is_open = (now.weekday() < 5) and (open_t <= now <= close_t)
            opens_str  = f"{hours['open'][0]:02d}:{hours['open'][1]:02d}"
            closes_str = f"{hours['close'][0]:02d}:{hours['close'][1]:02d}"
            result[market] = {
                "open":       is_open,
                "local_time": now.strftime("%H:%M"),
                "tz":         hours["tz"],
                "opens":      opens_str,
                "closes":     closes_str,
                "weekday":    now.weekday() < 5,
            }
        except Exception:
            result[market] = {"open": False}
    return result


def get_ticker_info(symbol: str) -> dict:
    try:
        info = yf.Ticker(_yf_symbol(symbol)).info
        return {
            "name":       info.get("longName", symbol),
            "sector":     info.get("sector", "N/A"),
            "market_cap": info.get("marketCap"),
            "pe_ratio":   info.get("trailingPE"),
            "52w_high":   info.get("fiftyTwoWeekHigh"),
            "52w_low":    info.get("fiftyTwoWeekLow"),
        }
    except Exception:
        return {}
