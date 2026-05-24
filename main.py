"""
StockBot — automated stock analysis & paper-trading bot.

Usage
-----
  python main.py              # run full analysis cycle once and show dashboard
  python main.py --loop       # run on a daily schedule (09:00 / 12:00 / 16:10 ET)
  python main.py --dashboard  # show current portfolio dashboard and exit
  python main.py --symbol AAPL # analyse a single symbol
  python main.py --mode simulation|alpaca_paper
"""

import argparse
import os
import sys
import time
from datetime import datetime

import schedule

import config
from database import db
from data import stock_data, news_scraper
from analysis import signals as sig_gen
from analysis import sentiment as sent
from trading import portfolio as pf
from trading import executor
from utils.logger import log

# Rich terminal UI
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich import box

console = Console()

BANNER = """
[bold cyan]
 ███████╗████████╗ ██████╗  ██████╗██╗  ██╗██████╗  ██████╗ ████████╗
 ██╔════╝╚══██╔══╝██╔═══██╗██╔════╝██║ ██╔╝██╔══██╗██╔═══██╗╚══██╔══╝
 ███████╗   ██║   ██║   ██║██║     █████╔╝ ██████╔╝██║   ██║   ██║
 ╚════██║   ██║   ██║   ██║██║     ██╔═██╗ ██╔══██╗██║   ██║   ██║
 ███████║   ██║   ╚██████╔╝╚██████╗██║  ██╗██████╔╝╚██████╔╝   ██║
 ╚══════╝   ╚═╝    ╚═════╝  ╚═════╝╚═╝  ╚═╝╚═════╝  ╚═════╝    ╚═╝
[/bold cyan]
[dim]AI-powered stock analysis & simulation trading[/dim]
"""


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def show_dashboard() -> None:
    console.clear()
    console.print(BANNER)

    prices = stock_data.get_batch_prices(config.WATCHLIST)
    port   = pf.summary(prices)

    # ── Portfolio summary ────────────────────────────────────────────────
    pnl_color = "green" if port["total_pnl"] >= 0 else "red"
    summary_table = Table(box=box.SIMPLE_HEAD, show_header=False)
    summary_table.add_column("Key",   style="dim")
    summary_table.add_column("Value", justify="right")
    summary_table.add_row("Mode",         f"[bold yellow]{config.TRADING_MODE.upper()}[/bold yellow]")
    summary_table.add_row("Cash",         f"[green]${port['cash']:,.2f}[/green]")
    summary_table.add_row("Market Value", f"${port['market_value']:,.2f}")
    summary_table.add_row("Total Value",  f"[bold]${port['total_value']:,.2f}[/bold]")
    summary_table.add_row("Total P&L",    f"[{pnl_color}]${port['total_pnl']:+,.2f} ({port['total_pnl_pct']:+.2f}%)[/{pnl_color}]")
    summary_table.add_row("Open Positions", str(port["n_positions"]))

    console.print(Panel(summary_table, title="[bold]Portfolio[/bold]", border_style="blue"))

    # ── Open positions ───────────────────────────────────────────────────
    if port["positions"]:
        pos_table = Table(box=box.SIMPLE_HEAD)
        pos_table.add_column("Symbol",  style="bold cyan")
        pos_table.add_column("Qty",     justify="right")
        pos_table.add_column("Avg Cost",justify="right")
        pos_table.add_column("Price",   justify="right")
        pos_table.add_column("Mkt Val", justify="right")
        pos_table.add_column("P&L",     justify="right")
        pos_table.add_column("P&L %",   justify="right")
        for p in port["positions"]:
            clr = "green" if p["unrealised_pnl"] >= 0 else "red"
            pos_table.add_row(
                p["symbol"],
                f"{p['quantity']:.0f}",
                f"${p['avg_cost']:.2f}",
                f"${p['current_price']:.2f}" if p["current_price"] else "N/A",
                f"${p['market_value']:.2f}",
                f"[{clr}]${p['unrealised_pnl']:+.2f}[/{clr}]",
                f"[{clr}]{p['pnl_pct']:+.1f}%[/{clr}]",
            )
        console.print(Panel(pos_table, title="[bold]Open Positions[/bold]", border_style="cyan"))

    # ── Latest signals ───────────────────────────────────────────────────
    recent_signals = db.get_recent_signals(15)
    if recent_signals:
        sig_table = Table(box=box.SIMPLE_HEAD)
        sig_table.add_column("Symbol",    style="bold")
        sig_table.add_column("Action",    justify="center")
        sig_table.add_column("Score",     justify="right")
        sig_table.add_column("RSI",       justify="right")
        sig_table.add_column("Sentiment", justify="right")
        sig_table.add_column("Generated", style="dim")
        for s in recent_signals:
            action = s["action"].upper()
            a_color = {"BUY": "green", "SELL": "red", "HOLD": "yellow"}.get(action, "white")
            sig_table.add_row(
                s["symbol"],
                f"[{a_color}]{action}[/{a_color}]",
                f"{s['score']:+.3f}",
                f"{s['rsi_value']:.1f}" if s["rsi_value"] else "—",
                f"{s['sentiment_avg']:+.2f}" if s["sentiment_avg"] is not None else "—",
                s["generated_at"][-8:],  # HH:MM:SS
            )
        console.print(Panel(sig_table, title="[bold]Latest Signals[/bold]", border_style="magenta"))

    # ── Recent trades ────────────────────────────────────────────────────
    trades = db.get_trades(8)
    if trades:
        tr_table = Table(box=box.SIMPLE_HEAD)
        tr_table.add_column("Symbol",  style="bold")
        tr_table.add_column("Side",    justify="center")
        tr_table.add_column("Qty",     justify="right")
        tr_table.add_column("Price",   justify="right")
        tr_table.add_column("Total",   justify="right")
        tr_table.add_column("P&L",     justify="right")
        tr_table.add_column("Time",    style="dim")
        for t in trades:
            side  = t["side"].upper()
            s_clr = "green" if side == "BUY" else "red"
            pnl   = t["pnl"]
            pnl_s = f"${pnl:+.2f}" if pnl is not None else "—"
            pnl_c = "green" if (pnl or 0) >= 0 else "red"
            tr_table.add_row(
                t["symbol"],
                f"[{s_clr}]{side}[/{s_clr}]",
                f"{t['quantity']:.0f}",
                f"${t['price']:.2f}",
                f"${t['total']:.2f}",
                f"[{pnl_c}]{pnl_s}[/{pnl_c}]",
                t["executed_at"][-8:],
            )
        console.print(Panel(tr_table, title="[bold]Recent Trades[/bold]", border_style="green"))

    # ── Recent news ──────────────────────────────────────────────────────
    news = db.get_recent_news(limit=12)
    if news:
        n_table = Table(box=box.SIMPLE_HEAD)
        n_table.add_column("Symbol", style="bold cyan", width=6)
        n_table.add_column("Title",  max_width=70)
        n_table.add_column("Sent",   justify="right", width=6)
        n_table.add_column("Source", style="dim", width=14)
        for n in news:
            s = n.get("sentiment") or 0
            s_clr = "green" if s > 0.1 else ("red" if s < -0.1 else "yellow")
            n_table.add_row(
                n["symbol"],
                n["title"][:70],
                f"[{s_clr}]{s:+.2f}[/{s_clr}]",
                (n["source"] or "")[:14],
            )
        console.print(Panel(n_table, title="[bold]Recent News[/bold]", border_style="yellow"))

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mkt_status = "[green]OPEN[/green]" if stock_data.is_market_open() else "[red]CLOSED[/red]"
    console.print(f"\n[dim]Last updated: {now_str}  |  Market: {mkt_status}[/dim]\n")


# ---------------------------------------------------------------------------
# Core analysis cycle
# ---------------------------------------------------------------------------

def run_analysis(symbols: list[str] | None = None) -> list[dict]:
    symbols = symbols or config.WATCHLIST
    console.print(f"\n[bold cyan]── Analysis cycle: {len(symbols)} symbols ──[/bold cyan]")

    all_signals = []
    for sym in symbols:
        console.print(f"  [dim]→[/dim] [bold]{sym}[/bold] ...", end=" ")

        # 1. Price data
        df = stock_data.fetch_ohlcv(sym)
        if df is None or len(df) < 30:
            console.print("[red]no price data[/red]")
            continue

        # 2. News
        articles = news_scraper.fetch_news(sym)
        # Annotate each article with its sentiment score for storage
        for a in articles:
            a["sentiment"] = sent.score_headline(
                f"{a.get('title','')}. {a.get('summary','')}"
            )
        db.save_news_batch(sym, articles)

        # 3. Generate signal
        signal = sig_gen.generate(sym, df, articles)
        signal["articles_count"] = len(articles)
        db.save_signal(signal)
        all_signals.append(signal)

        action = signal["action"].upper()
        score  = signal["score"]
        a_color = {"BUY": "green", "SELL": "red", "HOLD": "yellow"}.get(action, "white")
        console.print(f"[{a_color}]{action}[/{a_color}] score=[bold]{score:+.3f}[/bold]")

    return all_signals


# ---------------------------------------------------------------------------
# Trade execution cycle
# ---------------------------------------------------------------------------

def run_trading(signals: list[dict]) -> None:
    prices = {s["symbol"]: s.get("close") for s in signals if s.get("close")}

    # 1. Check stop-loss / take-profit for existing positions first
    sl_results = executor.check_stop_loss_take_profit(prices)
    if sl_results:
        console.print(f"\n[yellow]Stop-loss/take-profit triggered for {len(sl_results)} position(s)[/yellow]")

    # 2. Process new signals
    console.print("\n[bold cyan]── Trade execution ──[/bold cyan]")
    for s in signals:
        sym    = s["symbol"]
        action = s["action"]
        price  = s.get("close")
        if price is None:
            continue

        if action == "buy":
            result = executor.buy(sym, price, reason="; ".join(s.get("reasons", [])[:3]))
        elif action == "sell":
            result = executor.sell(sym, price, reason="; ".join(s.get("reasons", [])[:3]))
        else:
            continue

        status = "[green]✓[/green]" if result["ok"] else "[red]✗[/red]"
        console.print(f"  {status} {result['message']}")


# ---------------------------------------------------------------------------
# Full cycle
# ---------------------------------------------------------------------------

def full_cycle(symbols: list[str] | None = None) -> None:
    log.info("Starting full analysis+trade cycle")
    signals = run_analysis(symbols)
    if signals:
        run_trading(signals)
    show_dashboard()
    log.info("Cycle complete")


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def setup_schedule() -> None:
    schedule.every().day.at(config.SCHEDULE_ANALYSIS_TIME).do(full_cycle)
    schedule.every().day.at(config.SCHEDULE_TRADE_TIME).do(run_trading,
        signals=[])   # re-check existing positions
    schedule.every().day.at(config.SCHEDULE_MIDDAY_TIME).do(full_cycle)
    schedule.every().day.at(config.SCHEDULE_EOD_TIME).do(full_cycle)
    console.print(
        f"\n[green]Scheduler active.[/green] Next run at "
        f"[bold]{config.SCHEDULE_ANALYSIS_TIME}[/bold] ET every weekday.\n"
        "[dim]Press Ctrl+C to stop.[/dim]\n"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="StockBot — automated stock analysis & paper-trading bot"
    )
    parser.add_argument("--loop",      action="store_true", help="Run on daily schedule")
    parser.add_argument("--dashboard", action="store_true", help="Show dashboard and exit")
    parser.add_argument("--analyse",   action="store_true", help="Run analysis once (no trading)")
    parser.add_argument("--symbol",    type=str,            help="Analyse a single symbol")
    parser.add_argument(
        "--mode",
        choices=["simulation", "alpaca_paper", "alpaca_live"],
        help="Override trading mode from config",
    )
    args = parser.parse_args()

    # Override trading mode if given on CLI
    if args.mode:
        config.TRADING_MODE = args.mode
        os.environ["TRADING_MODE"] = args.mode

    # Initialise database
    db.init_db()

    symbols = [args.symbol.upper()] if args.symbol else None

    if args.dashboard:
        show_dashboard()
        return

    if args.analyse:
        run_analysis(symbols)
        show_dashboard()
        return

    if args.loop:
        # Run once immediately, then schedule
        full_cycle(symbols)
        setup_schedule()
        try:
            while True:
                schedule.run_pending()
                time.sleep(30)
        except KeyboardInterrupt:
            console.print("\n[yellow]Stopped.[/yellow]")
        return

    # Default: run one full cycle
    full_cycle(symbols)


if __name__ == "__main__":
    main()
