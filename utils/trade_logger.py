"""
trade_logger.py — persistent file-based logging for all bot activity.

Writes to logs/ directory (relative to project root):
  logs/trades_YYYY-MM-DD.jsonl      — every buy/sell with context
  logs/decisions_YYYY-MM-DD.jsonl   — every evaluation in each scan cycle
  logs/daily_YYYY-MM-DD.json        — end-of-day summary (overwritten each update)

Format: one JSON object per line (JSONL) so logs are streamable and grep-able.
"""

import json
import os
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _append(filename: str, record: dict) -> None:
    try:
        with open(LOG_DIR / filename, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass   # never let logging crash the bot


def log_trade(
    side:       str,
    symbol:     str,
    qty:        float,
    price:      float,
    pnl:        float | None = None,
    reason:     str          = "",
    score:      int          = 0,
    confidence: str          = "",
    exit_type:  str | None   = None,
) -> None:
    """Record a single trade execution."""
    _append(f"trades_{_today()}.jsonl", {
        "ts":         _now(),
        "side":       side.upper(),
        "symbol":     symbol,
        "qty":        qty,
        "price":      round(price, 2),
        "total":      round(qty * price, 2),
        "pnl":        round(pnl, 2) if pnl is not None else None,
        "score":      score,
        "confidence": confidence,
        "exit_type":  exit_type,
        "reason":     reason,
    })


def log_cycle(cycle_entries: list) -> None:
    """Append all decision entries from one 5-min scan cycle."""
    today = _today()
    for entry in cycle_entries:
        _append(f"decisions_{today}.jsonl", entry)


def log_daily_summary(pnl: float, trades: int, cash: float, n_positions: int) -> None:
    """Overwrite today's summary file (called after every cycle)."""
    path = LOG_DIR / f"daily_{_today()}.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "date":        _today(),
                "updated_at":  _now(),
                "total_pnl":   round(pnl, 2),
                "trades":      trades,
                "cash":        round(cash, 2),
                "n_positions": n_positions,
            }, f, indent=2)
    except Exception:
        pass


def read_recent_trades(days: int = 3) -> list[dict]:
    """Read trade log files from the last N days for the learning module."""
    from datetime import timedelta
    records = []
    for i in range(days):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        path = LOG_DIR / f"trades_{date}.jsonl"
        if not path.exists():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
        except Exception:
            pass
    return records
