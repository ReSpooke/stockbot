"""
learning.py — self-improving feedback loop for the decision engine.

The bot traces back its last N trades, measures which signal combinations
produced wins vs losses, and adjusts its aggressiveness accordingly.

Approach
────────
After each decision cycle the bot asks:
  1. Of my last 20 closed trades, what is my win rate?
  2. Which individual signals (OR breakout, VWAP, etc.) appeared most in winners?
  3. Should I tighten or loosen the BUY threshold?

Threshold adjustment rules
──────────────────────────
  win_rate ≥ 65%  → lower BUY_MIN to 4  (winning streak, trade more)
  win_rate ≥ 50%  → keep  BUY_MIN at 5  (neutral)
  win_rate ≥ 35%  → raise BUY_MIN to 6  (struggling, be picky)
  win_rate  < 35%  → raise BUY_MIN to 7  (losing streak, near-stop)

Signal accuracy (for display)
──────────────────────────────
  For each signal keyword ("OR breakout", "VWAP", "RSI", "MACD", "Volume"):
    accuracy = wins_with_signal / total_trades_with_signal

This is shown in the UI as a heatmap so the user can see which indicators
are actually working today.
"""

from __future__ import annotations
from utils.trade_logger import read_recent_trades

# Default threshold (also defined in decision.py BUY_MIN = 5)
_DEFAULT_BUY_MIN = 5

# Signals we track
_TRACKED_SIGNALS = ["OR breakout", "Above VWAP", "Below VWAP", "RSI", "MACD", "Volume"]


def get_performance() -> dict:
    """
    Analyse recent closed trades and return:
        buy_min      : adjusted BUY threshold for this session
        win_rate     : fraction of recent sells that were profitable
        n_trades     : number of closed trades analysed
        signal_stats : per-signal accuracy dict
        message      : human-readable summary of what the bot learned
    """
    trades = read_recent_trades(days=3)
    sells  = [t for t in trades if t.get("side") == "SELL" and t.get("pnl") is not None]

    if len(sells) < 3:
        return {
            "buy_min":      _DEFAULT_BUY_MIN,
            "win_rate":     None,
            "n_trades":     len(sells),
            "signal_stats": {},
            "message":      f"Not enough data yet ({len(sells)} closed trades). Using default threshold.",
        }

    wins     = [t for t in sells if (t.get("pnl") or 0) > 0]
    win_rate = len(wins) / len(sells)

    # ── Adjust aggressiveness ──────────────────────────────────────────────
    if win_rate >= 0.65:
        buy_min = 4
        mood    = "Winning streak — lowered entry bar to 4 (trade more aggressively)"
    elif win_rate >= 0.50:
        buy_min = 5
        mood    = f"Neutral {win_rate:.0%} win rate — holding threshold at 5"
    elif win_rate >= 0.35:
        buy_min = 6
        mood    = f"Struggling {win_rate:.0%} win rate — raised threshold to 6 (be picky)"
    else:
        buy_min = 7
        mood    = f"Losing streak {win_rate:.0%} — near-stop mode, threshold raised to 7"

    # ── Per-signal accuracy ────────────────────────────────────────────────
    # For buy trades: find matching sell to get P&L outcome
    buys = {t.get("symbol"): t for t in trades if t.get("side") == "BUY"}
    signal_stats: dict[str, dict] = {}

    for sell in sells:
        sym    = sell.get("symbol")
        buy_t  = buys.get(sym)
        if not buy_t:
            continue
        reason = buy_t.get("reason", "")
        won    = (sell.get("pnl") or 0) > 0

        for sig in _TRACKED_SIGNALS:
            if sig.lower() in reason.lower():
                if sig not in signal_stats:
                    signal_stats[sig] = {"wins": 0, "total": 0}
                signal_stats[sig]["total"] += 1
                if won:
                    signal_stats[sig]["wins"] += 1

    for sig, stat in signal_stats.items():
        stat["accuracy"] = round(stat["wins"] / stat["total"], 2) if stat["total"] else 0

    return {
        "buy_min":      buy_min,
        "win_rate":     round(win_rate, 3),
        "n_trades":     len(sells),
        "signal_stats": signal_stats,
        "message":      mood,
    }


# Cache the result for one scan cycle so we don't re-read files every stock
_cached: dict | None = None
_cache_count: int    = 0
_CACHE_EVERY: int    = 20   # recalculate every 20 stock evaluations


def get_buy_min() -> int:
    """Return the current adjusted BUY threshold (cached per cycle)."""
    global _cached, _cache_count
    _cache_count += 1
    if _cached is None or _cache_count >= _CACHE_EVERY:
        _cached      = get_performance()
        _cache_count = 0
    return _cached["buy_min"]


def invalidate_cache() -> None:
    """Call after each new trade so the next evaluation re-reads logs."""
    global _cached
    _cached = None
