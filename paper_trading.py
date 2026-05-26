#!/usr/bin/env python3
"""
paper_trading.py  —  Standalone paper-money trading simulator.

Runs the full analysis pipeline (price data → technical indicators →
news sentiment → signals) but executes trades against a VIRTUAL wallet.
Nothing touches real money. Completely isolated from the main stockbot.db.

Usage
-----
  python paper_trading.py              # one full cycle: analyse + trade + report
  python paper_trading.py --report     # show portfolio report only (no new trades)
  python paper_trading.py --reset      # wipe portfolio and start fresh
  python paper_trading.py --capital 50000   # change starting capital (on reset)
  python paper_trading.py --symbols RELIANCE TCS INFY   # analyse specific stocks
  python paper_trading.py --loop       # run continuously on a schedule
"""

import argparse
import json
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

# ── Third-party ────────────────────────────────────────────────────────────
from rich.console import Console
from rich.panel   import Panel
from rich.table   import Table
from rich.text    import Text
from rich         import box
from rich.rule    import Rule
from rich.columns import Columns

# ── Internal ───────────────────────────────────────────────────────────────
import config
from data    import stock_data, news_scraper
from analysis import signals as sig_gen, sentiment as sent
from utils.logger import log

# ══════════════════════════════════════════════════════════════════════════════
#  Constants
# ══════════════════════════════════════════════════════════════════════════════

PAPER_DB      = Path(__file__).parent / "paper_trades.db"
DEFAULT_CAP   = 20_000.0    # ₹20,000 intraday capital
CURRENCY      = "₹"

console = Console()


# ══════════════════════════════════════════════════════════════════════════════
#  Database layer  (fully self-contained, no relation to stockbot.db)
# ══════════════════════════════════════════════════════════════════════════════

@contextmanager
def _conn():
    con = sqlite3.connect(str(PAPER_DB))
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _init_db(initial_capital: float = DEFAULT_CAP) -> None:
    with _conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS portfolio (
            id              INTEGER PRIMARY KEY,
            cash            REAL    NOT NULL,
            initial_capital REAL    NOT NULL,
            created_at      TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS positions (
            symbol      TEXT PRIMARY KEY,
            quantity    REAL NOT NULL,
            avg_cost    REAL NOT NULL,
            opened_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT    NOT NULL,
            side        TEXT    NOT NULL,
            quantity    REAL    NOT NULL,
            price       REAL    NOT NULL,
            total       REAL    NOT NULL,
            pnl         REAL,
            reason      TEXT,
            executed_at TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_snapshots (
            date        TEXT PRIMARY KEY,
            total_value REAL NOT NULL,
            cash        REAL NOT NULL,
            n_positions INTEGER NOT NULL
        );
        """)
        row = con.execute("SELECT id FROM portfolio").fetchone()
        if row is None:
            con.execute(
                "INSERT INTO portfolio (cash, initial_capital, created_at) VALUES (?,?,?)",
                (initial_capital, initial_capital, _now()),
            )


def _reset_db(initial_capital: float = DEFAULT_CAP) -> None:
    with _conn() as con:
        con.executescript("DROP TABLE IF EXISTS portfolio; DROP TABLE IF EXISTS positions; DROP TABLE IF EXISTS trades; DROP TABLE IF EXISTS daily_snapshots;")
    _init_db(initial_capital)


# ── Portfolio reads/writes ────────────────────────────────────────────────

def _get_cash() -> float:
    with _conn() as con:
        row = con.execute("SELECT cash FROM portfolio").fetchone()
        return float(row["cash"]) if row else DEFAULT_CAP


def _get_initial_capital() -> float:
    with _conn() as con:
        row = con.execute("SELECT initial_capital FROM portfolio").fetchone()
        return float(row["initial_capital"]) if row else DEFAULT_CAP


def _set_cash(amount: float) -> None:
    with _conn() as con:
        con.execute("UPDATE portfolio SET cash = ?", (amount,))


def _get_positions() -> dict:
    with _conn() as con:
        rows = con.execute("SELECT * FROM positions").fetchall()
        return {r["symbol"]: dict(r) for r in rows}


def _upsert_position(symbol: str, qty: float, avg_cost: float) -> None:
    now = _now()
    with _conn() as con:
        if con.execute("SELECT 1 FROM positions WHERE symbol=?", (symbol,)).fetchone():
            con.execute("UPDATE positions SET quantity=?, avg_cost=? WHERE symbol=?",
                        (qty, avg_cost, symbol))
        else:
            con.execute("INSERT INTO positions (symbol,quantity,avg_cost,opened_at) VALUES (?,?,?,?)",
                        (symbol, qty, avg_cost, now))


def _delete_position(symbol: str) -> None:
    with _conn() as con:
        con.execute("DELETE FROM positions WHERE symbol=?", (symbol,))


def _save_trade(symbol, side, qty, price, pnl=None, reason="") -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO trades (symbol,side,quantity,price,total,pnl,reason,executed_at) VALUES (?,?,?,?,?,?,?,?)",
            (symbol, side, qty, price, qty * price, pnl, reason, _now()),
        )


def _get_trades(limit: int = 100) -> list:
    with _conn() as con:
        return [dict(r) for r in
                con.execute("SELECT * FROM trades ORDER BY executed_at DESC LIMIT ?", (limit,)).fetchall()]


def _snapshot_today(total_value: float, cash: float, n_positions: int) -> None:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO daily_snapshots (date,total_value,cash,n_positions) VALUES (?,?,?,?)",
            (today, total_value, cash, n_positions),
        )


def _get_snapshots() -> list:
    with _conn() as con:
        return [dict(r) for r in
                con.execute("SELECT * FROM daily_snapshots ORDER BY date ASC").fetchall()]


# ══════════════════════════════════════════════════════════════════════════════
#  Trade execution  (paper only — no real API calls)
# ══════════════════════════════════════════════════════════════════════════════

def _can_buy(symbol: str, price: float) -> tuple:
    positions = _get_positions()
    if symbol in positions:
        return False, "Already holding"
    if len(positions) >= config.MAX_POSITIONS:
        return False, f"Max {config.MAX_POSITIONS} positions reached"
    if _get_cash() < price:
        return False, "Not enough cash"
    return True, ""


def _position_size(price: float) -> int:
    cash      = _get_cash()
    positions = _get_positions()
    total     = cash + sum(float(p["avg_cost"]) * float(p["quantity"]) for p in positions.values())
    max_spend = min(total * config.MAX_POSITION_PCT, cash * 0.95)
    return max(1, int(max_spend / price)) if price > 0 else 0


def paper_buy(symbol: str, price: float, reason: str = "") -> dict:
    ok, msg = _can_buy(symbol, price)
    if not ok:
        return {"ok": False, "symbol": symbol, "side": "buy", "msg": msg}
    qty  = _position_size(price)
    cost = qty * price
    if cost > _get_cash():
        qty  = int(_get_cash() / price)
        cost = qty * price
    if qty <= 0:
        return {"ok": False, "symbol": symbol, "side": "buy", "msg": "Qty = 0"}
    _set_cash(_get_cash() - cost)
    _upsert_position(symbol, qty, price)
    _save_trade(symbol, "buy", qty, price, reason=reason)
    return {"ok": True, "symbol": symbol, "side": "buy",
            "qty": qty, "price": price, "total": cost,
            "msg": f"Bought {qty} × {symbol} @ {CURRENCY}{price:.2f}  (cost {CURRENCY}{cost:,.2f})"}


def paper_sell(symbol: str, price: float, reason: str = "") -> dict:
    positions = _get_positions()
    if symbol not in positions:
        return {"ok": False, "symbol": symbol, "side": "sell", "msg": "No position"}
    qty      = float(positions[symbol]["quantity"])
    avg_cost = float(positions[symbol]["avg_cost"])
    pnl      = (price - avg_cost) * qty
    _set_cash(_get_cash() + qty * price)
    _delete_position(symbol)
    _save_trade(symbol, "sell", qty, price, pnl=pnl, reason=reason)
    return {"ok": True, "symbol": symbol, "side": "sell",
            "qty": qty, "price": price, "pnl": pnl,
            "msg": f"Sold {qty} × {symbol} @ {CURRENCY}{price:.2f}  P&L: {CURRENCY}{pnl:+,.2f}"}


def check_sl_tp(prices: dict) -> list:
    """Auto-close positions that hit stop-loss or take-profit."""
    results = []
    for sym, pos in _get_positions().items():
        price = prices.get(sym)
        if not price:
            continue
        avg   = float(pos["avg_cost"])
        chg   = (price - avg) / avg
        if chg <= -config.STOP_LOSS_PCT:
            r = paper_sell(sym, price, f"Stop-loss ({chg*100:.1f}%)")
            results.append(r)
        elif chg >= config.TAKE_PROFIT_PCT:
            r = paper_sell(sym, price, f"Take-profit ({chg*100:.1f}%)")
            results.append(r)
    return results


# ══════════════════════════════════════════════════════════════════════════════
#  Portfolio maths
# ══════════════════════════════════════════════════════════════════════════════

def portfolio_summary(prices: dict) -> dict:
    cash      = _get_cash()
    initial   = _get_initial_capital()
    positions = _get_positions()

    mkt_value = 0.0
    pos_list  = []
    for sym, pos in positions.items():
        qty  = float(pos["quantity"])
        avg  = float(pos["avg_cost"])
        px   = prices.get(sym, avg)
        mv   = px * qty
        pnl  = (px - avg) * qty
        pct  = ((px - avg) / avg) * 100
        mkt_value += mv
        pos_list.append({"symbol": sym, "qty": qty, "avg": avg, "price": px,
                         "mv": mv, "pnl": pnl, "pct": pct})

    total     = cash + mkt_value
    total_pnl = total - initial
    total_pct = (total_pnl / initial) * 100

    trades    = _get_trades()
    closed    = [t for t in trades if t["side"] == "sell" and t["pnl"] is not None]
    wins      = [t for t in closed if (t["pnl"] or 0) > 0]
    losses    = [t for t in closed if (t["pnl"] or 0) < 0]
    win_rate  = len(wins) / len(closed) * 100 if closed else 0
    best      = max((t["pnl"] or 0) for t in closed) if closed else 0
    worst     = min((t["pnl"] or 0) for t in closed) if closed else 0
    avg_pnl   = sum(t["pnl"] or 0 for t in closed) / len(closed) if closed else 0

    return {
        "cash": cash, "mkt_value": mkt_value, "total": total,
        "initial": initial, "total_pnl": total_pnl, "total_pct": total_pct,
        "positions": pos_list, "n_positions": len(pos_list),
        "n_trades": len(trades), "n_closed": len(closed),
        "win_rate": win_rate, "wins": len(wins), "losses": len(losses),
        "best_trade": best, "worst_trade": worst, "avg_pnl": avg_pnl,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Rich terminal dashboard
# ══════════════════════════════════════════════════════════════════════════════

def print_dashboard(prices: dict, signals: list = None) -> None:
    console.clear()

    # ── Banner ────────────────────────────────────────────────────────────
    console.print()
    console.print(Rule("[bold cyan]  StockBot — Paper Trading Simulator  [/bold cyan]", style="cyan"))
    console.print(f"  [dim]{datetime.now().strftime('%A, %d %B %Y  %H:%M:%S')}  |  Mode: [bold yellow]PAPER MONEY[/bold yellow]  |  All trades are virtual — no real money at risk[/dim]")
    console.print()

    s = portfolio_summary(prices)
    pnl_color = "bright_green" if s["total_pnl"] >= 0 else "bright_red"
    sign      = "+" if s["total_pnl"] >= 0 else ""

    # ── Summary cards (side by side) ─────────────────────────────────────
    def _card(title, value, sub="", sub_color="dim"):
        t = Table(box=None, show_header=False, padding=(0,1))
        t.add_column(width=24)
        t.add_row(f"[dim]{title}[/dim]")
        t.add_row(f"[bold white]{value}[/bold white]")
        if sub:
            t.add_row(f"[{sub_color}]{sub}[/{sub_color}]")
        return Panel(t, border_style="grey30", padding=(0,1))

    cards = [
        _card("TOTAL VALUE",
              f"{CURRENCY}{s['total']:>12,.2f}",
              f"{sign}{CURRENCY}{abs(s['total_pnl']):,.2f}  ({sign}{s['total_pct']:.2f}%)",
              pnl_color),
        _card("CASH",        f"{CURRENCY}{s['cash']:>12,.2f}", "Available to deploy"),
        _card("MARKET VALUE",f"{CURRENCY}{s['mkt_value']:>12,.2f}", f"{s['n_positions']} open position(s)"),
        _card("PERFORMANCE",
              f"Win rate {s['win_rate']:.0f}%",
              f"{s['n_closed']} closed  |  {s['wins']}W  {s['losses']}L",
              "dim"),
    ]
    console.print(Columns(cards, equal=True, expand=True))
    console.print()

    # ── Open positions ────────────────────────────────────────────────────
    if s["positions"]:
        t = Table(title="[bold]Open Positions[/bold]", box=box.SIMPLE_HEAD,
                  title_justify="left", border_style="grey30")
        t.add_column("Symbol",  style="bold cyan", width=14)
        t.add_column("Qty",     justify="right", width=8)
        t.add_column("Avg Cost",justify="right", width=14, style="dim")
        t.add_column("Price",   justify="right", width=14)
        t.add_column("Value",   justify="right", width=14)
        t.add_column("P&L",     justify="right", width=16)
        t.add_column("P&L %",   justify="right", width=10)
        for p in s["positions"]:
            c = "bright_green" if p["pnl"] >= 0 else "bright_red"
            sg = "+" if p["pnl"] >= 0 else ""
            t.add_row(
                p["symbol"],
                f"{p['qty']:.0f}",
                f"{CURRENCY}{p['avg']:.2f}",
                f"{CURRENCY}{p['price']:.2f}",
                f"{CURRENCY}{p['mv']:,.2f}",
                f"[{c}]{sg}{CURRENCY}{abs(p['pnl']):,.2f}[/{c}]",
                f"[{c}]{sg}{p['pct']:.2f}%[/{c}]",
            )
        console.print(t)
        console.print()

    # ── Latest signals ────────────────────────────────────────────────────
    if signals:
        t = Table(title="[bold]Latest Signals[/bold]", box=box.SIMPLE_HEAD,
                  title_justify="left", border_style="grey30")
        t.add_column("Symbol",    style="bold", width=14)
        t.add_column("Action",    justify="center", width=10)
        t.add_column("Score",     justify="right",  width=10)
        t.add_column("RSI",       justify="right",  width=8)
        t.add_column("Sentiment", justify="right",  width=12)
        t.add_column("Key reason", width=40)
        for s_ in signals:
            ac = s_["action"].upper()
            ac_style = {"BUY": "bold bright_green", "SELL": "bold bright_red", "HOLD": "bold yellow"}.get(ac, "white")
            rsi  = s_.get("rsi_value")
            rsi_str = f"{rsi:.1f}" if rsi else "—"
            rsi_style = "bright_green" if (rsi and rsi <= 35) else ("bright_red" if (rsi and rsi >= 65) else "white")
            sent_v = s_.get("sentiment_avg", 0) or 0
            sent_style = "bright_green" if sent_v > 0.1 else ("bright_red" if sent_v < -0.1 else "dim")
            reasons = s_.get("reasons") or []
            if isinstance(reasons, str):
                import json as _j
                try: reasons = _j.loads(reasons)
                except Exception: reasons = [reasons]
            first_reason = reasons[0] if reasons else "—"
            t.add_row(
                s_["symbol"],
                f"[{ac_style}]{ac}[/{ac_style}]",
                f"{s_['score']:+.3f}",
                f"[{rsi_style}]{rsi_str}[/{rsi_style}]",
                f"[{sent_style}]{sent_v:+.2f}[/{sent_style}]",
                f"[dim]{first_reason[:40]}[/dim]",
            )
        console.print(t)
        console.print()

    # ── Trade history ─────────────────────────────────────────────────────
    trades = _get_trades(20)
    if trades:
        t = Table(title="[bold]Trade History[/bold]", box=box.SIMPLE_HEAD,
                  title_justify="left", border_style="grey30")
        t.add_column("Symbol", style="bold",     width=14)
        t.add_column("Side",   justify="center", width=8)
        t.add_column("Qty",    justify="right",  width=8)
        t.add_column("Price",  justify="right",  width=14)
        t.add_column("Total",  justify="right",  width=14)
        t.add_column("P&L",    justify="right",  width=14)
        t.add_column("Reason", width=34, overflow="fold")
        t.add_column("Time",   style="dim", width=18)
        for tr in trades:
            side_style = "bright_green" if tr["side"] == "buy" else "bright_red"
            pnl  = tr["pnl"]
            pnl_str = f"{CURRENCY}{pnl:+,.2f}" if pnl is not None else "—"
            pnl_clr = "bright_green" if (pnl or 0) >= 0 else "bright_red"
            t.add_row(
                tr["symbol"],
                f"[{side_style}]{tr['side'].upper()}[/{side_style}]",
                f"{tr['quantity']:.0f}",
                f"{CURRENCY}{tr['price']:.2f}",
                f"{CURRENCY}{tr['total']:,.2f}",
                f"[{pnl_clr}]{pnl_str}[/{pnl_clr}]",
                f"[dim]{(tr['reason'] or '—')[:34]}[/dim]",
                tr["executed_at"][:16],
            )
        console.print(t)
        console.print()

    # ── Stats footer ──────────────────────────────────────────────────────
    if s["n_closed"]:
        console.print(
            f"  [dim]Best trade: [bright_green]+{CURRENCY}{s['best_trade']:,.2f}[/bright_green]  |  "
            f"Worst trade: [bright_red]{CURRENCY}{s['worst_trade']:,.2f}[/bright_red]  |  "
            f"Avg closed P&L: {CURRENCY}{s['avg_pnl']:+,.2f}[/dim]"
        )
        console.print()

    console.print(Rule(style="grey23"))
    mkt_open = stock_data.is_market_open()
    mkt_str  = "[bright_green]OPEN[/bright_green]" if mkt_open else "[red]CLOSED[/red]"
    console.print(f"  [dim]Market: {mkt_str}  |  DB: {PAPER_DB.name}  |  "
                  f"Paper DB is separate from main stockbot.db[/dim]")
    console.print()


# ══════════════════════════════════════════════════════════════════════════════
#  Main analysis + trade cycle
# ══════════════════════════════════════════════════════════════════════════════

def run_cycle(symbols: list) -> list:
    """Fetch data → analyse → execute paper trades. Returns list of signals."""
    signals = []
    console.print(f"\n[bold cyan]── Paper trading cycle: {len(symbols)} symbols ──[/bold cyan]")

    prices = stock_data.get_batch_prices(symbols)

    # 1. Stop-loss / take-profit sweep first
    sl_results = check_sl_tp(prices)
    if sl_results:
        console.print()
        for r in sl_results:
            icon = "[bright_green]✓[/bright_green]" if r["ok"] else "[red]✗[/red]"
            console.print(f"  {icon} SL/TP: {r['msg']}")

    # 2. Per-symbol analysis
    console.print()
    for sym in symbols:
        console.print(f"  [dim]▸[/dim] [bold]{sym}[/bold]", end=" ")

        df = stock_data.fetch_ohlcv(sym)
        if df is None or len(df) < 30:
            console.print("[dim]skip (no data)[/dim]")
            continue

        articles = news_scraper.fetch_news(sym)
        for a in articles:
            a["sentiment"] = sent.score_headline(
                f"{a.get('title','')}. {a.get('summary','')}"
            )

        signal = sig_gen.generate(sym, df, articles)
        signals.append(signal)

        action = signal["action"].upper()
        score  = signal["score"]
        price  = prices.get(sym) or signal.get("close")

        color  = {"BUY": "bright_green", "SELL": "bright_red", "HOLD": "yellow"}.get(action, "white")
        console.print(f"[{color}]{action}[/{color}] score=[bold]{score:+.3f}[/bold]", end="  ")

        # 3. Execute paper trade
        if price:
            if action == "buy":
                r = paper_buy(sym, price, reason="; ".join((signal.get("reasons") or [])[:2]))
                console.print(f"→ [bright_green]{r['msg']}[/bright_green]" if r["ok"]
                              else f"→ [dim]{r['msg']}[/dim]")
            elif action == "sell":
                r = paper_sell(sym, price, reason="; ".join((signal.get("reasons") or [])[:2]))
                pnl_c = "bright_green" if r.get("pnl",0) >= 0 else "bright_red"
                console.print(f"→ [{pnl_c}]{r['msg']}[/{pnl_c}]" if r["ok"]
                              else f"→ [dim]{r['msg']}[/dim]")
            else:
                console.print()
        else:
            console.print()

    # 4. Daily snapshot
    fresh_prices = stock_data.get_batch_prices(symbols)
    s = portfolio_summary(fresh_prices)
    _snapshot_today(s["total"], s["cash"], s["n_positions"])

    return signals


# ══════════════════════════════════════════════════════════════════════════════
#  CLI entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="StockBot paper trading simulator — virtual money, real market data"
    )
    parser.add_argument("--reset",   action="store_true",
                        help="Wipe all paper trades and restart with fresh capital")
    parser.add_argument("--capital", type=float, default=DEFAULT_CAP,
                        help=f"Starting capital on reset (default ₹{DEFAULT_CAP:,.0f})")
    parser.add_argument("--report",  action="store_true",
                        help="Show portfolio report only — no new analysis")
    parser.add_argument("--loop",    action="store_true",
                        help="Keep running on a daily schedule")
    parser.add_argument("--symbols", nargs="+",
                        help="Analyse specific symbols (default: full watchlist)")
    args = parser.parse_args()

    # Handle reset
    if args.reset:
        confirm = input(f"  Reset paper portfolio to {CURRENCY}{args.capital:,.0f}? [y/N] ").strip().lower()
        if confirm != "y":
            console.print("[dim]Aborted.[/dim]")
            sys.exit(0)
        _reset_db(args.capital)
        console.print(f"[bright_green]✓ Paper portfolio reset to {CURRENCY}{args.capital:,.0f}[/bright_green]")
        return

    # Ensure DB exists
    _init_db()

    symbols = [s.upper() for s in args.symbols] if args.symbols else config.WATCHLIST

    # Report-only mode
    if args.report:
        prices = stock_data.get_batch_prices(symbols)
        print_dashboard(prices)
        return

    # Single cycle (default)
    if not args.loop:
        signals = run_cycle(symbols)
        prices  = stock_data.get_batch_prices(symbols)
        print_dashboard(prices, signals)
        return

    # Scheduled loop
    import schedule as sched
    console.print("\n[bold green]Paper trading loop started.[/bold green]")
    console.print(f"[dim]Analysis runs at {config.SCHEDULE_ANALYSIS_TIME}, "
                  f"{config.SCHEDULE_MIDDAY_TIME}, and {config.SCHEDULE_EOD_TIME} IST.[/dim]")
    console.print("[dim]Press Ctrl+C to stop.[/dim]\n")

    def _job():
        signals = run_cycle(symbols)
        prices  = stock_data.get_batch_prices(symbols)
        print_dashboard(prices, signals)

    sched.every().day.at(config.SCHEDULE_ANALYSIS_TIME).do(_job)
    sched.every().day.at(config.SCHEDULE_MIDDAY_TIME).do(_job)
    sched.every().day.at(config.SCHEDULE_EOD_TIME).do(_job)

    _job()   # run once immediately
    try:
        while True:
            sched.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        console.print("\n[yellow]Paper trading loop stopped.[/yellow]")


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    main()
