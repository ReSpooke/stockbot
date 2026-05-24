"""SQLite persistence layer for portfolio, trades, signals, and news."""

import sqlite3
import json
from datetime import datetime
from contextlib import contextmanager
from typing import Any

import config
from utils.logger import log


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

@contextmanager
def _conn():
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def init_db() -> None:
    with _conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS portfolio (
            id          INTEGER PRIMARY KEY,
            cash        REAL    NOT NULL DEFAULT 100000.0,
            updated_at  TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS positions (
            symbol      TEXT    PRIMARY KEY,
            quantity    REAL    NOT NULL,
            avg_cost    REAL    NOT NULL,
            opened_at   TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT    NOT NULL,
            side        TEXT    NOT NULL,   -- 'buy' | 'sell'
            quantity    REAL    NOT NULL,
            price       REAL    NOT NULL,
            total       REAL    NOT NULL,
            pnl         REAL,              -- only for sells
            reason      TEXT,
            executed_at TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS signals (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol         TEXT    NOT NULL,
            action         TEXT    NOT NULL,   -- 'buy' | 'sell' | 'hold'
            score          REAL    NOT NULL,
            rsi_score      REAL,
            macd_score     REAL,
            sma_score      REAL,
            bb_score       REAL,
            volume_score   REAL,
            sentiment_score REAL,
            rsi_value      REAL,
            sentiment_avg  REAL,
            reasons        TEXT,
            generated_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS news (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol       TEXT NOT NULL,
            title        TEXT NOT NULL,
            source       TEXT,
            url          TEXT,
            sentiment    REAL,
            published_at TEXT,
            fetched_at   TEXT NOT NULL
        );
        """)

    # seed portfolio row if missing
    with _conn() as con:
        row = con.execute("SELECT id FROM portfolio").fetchone()
        if row is None:
            con.execute(
                "INSERT INTO portfolio (cash, updated_at) VALUES (?, ?)",
                (config.INITIAL_CAPITAL, _now()),
            )
    log.debug("Database initialised at %s", config.DB_PATH)


# ---------------------------------------------------------------------------
# Portfolio helpers
# ---------------------------------------------------------------------------

def get_cash() -> float:
    with _conn() as con:
        row = con.execute("SELECT cash FROM portfolio").fetchone()
        return float(row["cash"]) if row else config.INITIAL_CAPITAL


def set_cash(amount: float) -> None:
    with _conn() as con:
        con.execute("UPDATE portfolio SET cash = ?, updated_at = ?", (amount, _now()))


def get_positions() -> dict[str, dict]:
    with _conn() as con:
        rows = con.execute("SELECT * FROM positions").fetchall()
        return {r["symbol"]: dict(r) for r in rows}


def upsert_position(symbol: str, quantity: float, avg_cost: float) -> None:
    now = _now()
    with _conn() as con:
        existing = con.execute(
            "SELECT quantity FROM positions WHERE symbol = ?", (symbol,)
        ).fetchone()
        if existing:
            con.execute(
                "UPDATE positions SET quantity=?, avg_cost=?, updated_at=? WHERE symbol=?",
                (quantity, avg_cost, now, symbol),
            )
        else:
            con.execute(
                "INSERT INTO positions (symbol, quantity, avg_cost, opened_at, updated_at) VALUES (?,?,?,?,?)",
                (symbol, quantity, avg_cost, now, now),
            )


def delete_position(symbol: str) -> None:
    with _conn() as con:
        con.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))


# ---------------------------------------------------------------------------
# Trade helpers
# ---------------------------------------------------------------------------

def save_trade(
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    pnl: float | None = None,
    reason: str = "",
) -> None:
    with _conn() as con:
        con.execute(
            """INSERT INTO trades (symbol, side, quantity, price, total, pnl, reason, executed_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (symbol, side, quantity, price, quantity * price, pnl, reason, _now()),
        )


def get_trades(limit: int = 50) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM trades ORDER BY executed_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Signal helpers
# ---------------------------------------------------------------------------

def save_signal(signal: dict) -> None:
    with _conn() as con:
        con.execute(
            """INSERT INTO signals
               (symbol, action, score, rsi_score, macd_score, sma_score,
                bb_score, volume_score, sentiment_score, rsi_value,
                sentiment_avg, reasons, generated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                signal.get("symbol"),
                signal.get("action"),
                signal.get("score"),
                signal.get("rsi_score"),
                signal.get("macd_score"),
                signal.get("sma_score"),
                signal.get("bb_score"),
                signal.get("volume_score"),
                signal.get("sentiment_score"),
                signal.get("rsi_value"),
                signal.get("sentiment_avg"),
                json.dumps(signal.get("reasons", [])),
                _now(),
            ),
        )


def get_recent_signals(limit: int = 30) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM signals ORDER BY generated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# News helpers
# ---------------------------------------------------------------------------

def save_news_batch(symbol: str, articles: list[dict]) -> None:
    now = _now()
    with _conn() as con:
        for a in articles:
            con.execute(
                """INSERT OR IGNORE INTO news
                   (symbol, title, source, url, sentiment, published_at, fetched_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    symbol,
                    a.get("title", ""),
                    a.get("source", ""),
                    a.get("url", ""),
                    a.get("sentiment"),
                    a.get("published_at", ""),
                    now,
                ),
            )


def get_recent_news(symbol: str | None = None, limit: int = 40) -> list[dict]:
    with _conn() as con:
        if symbol:
            rows = con.execute(
                "SELECT * FROM news WHERE symbol=? ORDER BY fetched_at DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM news ORDER BY fetched_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
