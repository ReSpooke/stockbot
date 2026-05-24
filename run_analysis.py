# -*- coding: utf-8 -*-
"""
run_analysis.py — live paper trading test runner.
Runs full analysis on all watchlist stocks, executes paper trades,
then prints a detailed report of results.

Usage: python run_analysis.py
"""
import sys
import io

# Force UTF-8 output on Windows so box-drawing chars don't crash
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import paper_trading
import config
from data import stock_data, news_scraper
from analysis import signals as sig_gen, sentiment as sent, ml_model

DIVIDER = "=" * 65

def _cur(sym):
    """Return currency prefix for a symbol."""
    return "Rs" if config.SYMBOL_MARKET.get(sym, config.MARKET) == "NSE" else "$"


def run():
    nse_syms = [s for s in config.WATCHLIST if config.SYMBOL_MARKET.get(s, "NSE") == "NSE"]
    us_syms  = [s for s in config.WATCHLIST if config.SYMBOL_MARKET.get(s, "NSE") != "NSE"]

    # Auto-train ML model if missing or stale
    ml_model.retrain_if_stale()
    ml_meta = ml_model.get_meta()
    ml_status = (
        f"trained {ml_meta.get('trained_at','?')[:10]}  acc={ml_meta.get('accuracy',0)*100:.1f}%  "
        f"samples={ml_meta.get('n_samples','?')}"
        if ml_meta else "NOT TRAINED (run: python -c \"from analysis.ml_model import train; train()\")"
    )

    print(f"\n{DIVIDER}")
    print("  STOCKBOT  --  PAPER TRADING  --  LIVE ANALYSIS RUN")
    print(f"  NSE: {len(nse_syms)} stocks  |  US: {len(us_syms)} stocks  |  Total: {len(config.WATCHLIST)}")
    print(f"  Capital: Rs{paper_trading._get_initial_capital():,.0f}")
    print(f"  ML Model: {ml_status}")
    print(DIVIDER)

    all_signals = []
    prices      = {}

    for sym in config.WATCHLIST:
        mkt = config.SYMBOL_MARKET.get(sym, config.MARKET)
        print(f"\n[{sym}] ({mkt})")

        df = stock_data.fetch_ohlcv(sym)
        if df is None or len(df) < 30:
            print("  SKIP -- could not fetch enough price data")
            continue

        last_close = float(df["Close"].iloc[-1])
        prices[sym] = last_close
        print(f"  Price data : {len(df)} rows  |  Last close : {_cur(sym)}{last_close:,.2f}")

        articles = news_scraper.fetch_news(sym)
        for a in articles:
            a["sentiment"] = sent.score_headline(a.get("title", ""))
        avg_sent = sum(a["sentiment"] for a in articles) / max(len(articles), 1)
        print(f"  News found : {len(articles)} articles  |  Avg sentiment : {avg_sent:+.3f}")
        for a in articles[:3]:
            print(f"    [{a['sentiment']:+.2f}] {a['title'][:72]}")

        signal = sig_gen.generate(sym, df, articles)
        all_signals.append(signal)

        rsi = signal.get("rsi_value") or 0
        regime     = signal.get("regime", "?")
        regime_vol = signal.get("regime_vol_ann", 0)
        buy_thr    = signal.get("buy_threshold", config.BUY_THRESHOLD)
        sell_thr   = signal.get("sell_threshold", config.SELL_THRESHOLD)
        ml_s       = signal.get("ml_score", 0)
        ml_pb      = signal.get("ml_p_buy", 0)
        ml_ps      = signal.get("ml_p_sell", 0)
        print(f"  SIGNAL  >> {signal['action'].upper():5}  score={signal['score']:+.4f}  "
              f"RSI={rsi:.1f}  mom={signal.get('momentum_score',0):+.3f}  "
              f"sent={signal.get('sentiment_avg',0):+.3f}  mkt={mkt}")
        print(f"  Regime  :  {regime.upper():8}  trend={signal.get('regime_trend_pct',0):+.1f}%  "
              f"vol_ann={regime_vol:.0f}%  "
              f"thresholds={buy_thr:.3f}/{sell_thr:.3f}")
        print(f"  ML      :  score={ml_s:+.3f}  P(buy)={ml_pb:.2f}  P(sell)={ml_ps:.2f}  "
              f"{'[ACTIVE]' if signal.get('ml_trained') else '[not trained]'}")
        print(f"  Scores  :  RSI={signal.get('rsi_score',0):+.3f}  "
              f"MACD={signal.get('macd_score',0):+.3f}  "
              f"SMA={signal.get('sma_score',0):+.3f}  "
              f"BB={signal.get('bb_score',0):+.3f}  "
              f"Vol={signal.get('volume_score',0):+.3f}  "
              f"Mom={signal.get('momentum_score',0):+.3f}  "
              f"Sent={signal.get('sentiment_score',0):+.3f}  "
              f"ML={signal.get('ml_score',0):+.3f}")
        for r in (signal.get("reasons") or [])[:4]:
            safe = r.encode("ascii", "replace").decode("ascii")
            print(f"    >> {safe}")

    # ── Execute paper trades ─────────────────────────────────────────────
    print(f"\n{DIVIDER}")
    print("  EXECUTING PAPER TRADES")
    print(DIVIDER)

    sl_results = paper_trading.check_sl_tp(prices)
    for r in sl_results:
        tag = "OK  " if r["ok"] else "SKIP"
        print(f"  [{tag}] SL/TP: {r['msg']}")

    bought, sold, held = [], [], []
    for s in all_signals:
        sym    = s["symbol"]
        action = s["action"]
        price  = prices.get(sym)
        if not price:
            continue
        if action == "buy":
            r = paper_trading.paper_buy(sym, price,
                reason="; ".join(str(x) for x in (s.get("reasons") or [])[:2]))
            tag = "OK  " if r["ok"] else "SKIP"
            print(f"  [{tag}] BUY  {sym}: {r['msg']}")
            if r["ok"]: bought.append(sym)
        elif action == "sell":
            r = paper_trading.paper_sell(sym, price,
                reason="; ".join(str(x) for x in (s.get("reasons") or [])[:2]))
            pnl = r.get("pnl", 0) or 0
            tag = "OK  " if r["ok"] else "SKIP"
            print(f"  [{tag}] SELL {sym}: {r['msg']}")
            if r["ok"]: sold.append(sym)
        else:
            print(f"  [HOLD] {sym:12}  score={s['score']:+.3f}")
            held.append(sym)

    # ── Portfolio snapshot ────────────────────────────────────────────────
    fresh_prices = stock_data.get_batch_prices(config.WATCHLIST)
    prices.update(fresh_prices)

    port = paper_trading.portfolio_summary(prices)
    paper_trading._snapshot_today(port["total"], port["cash"], port["n_positions"])

    print(f"\n{DIVIDER}")
    print("  PORTFOLIO SNAPSHOT AFTER TRADES")
    print(DIVIDER)
    print(f"  Initial capital   : Rs{port['initial']:>12,.2f}  (base currency)")
    print(f"  Cash remaining    : Rs{port['cash']:>12,.2f}")
    print(f"  Market value      : Rs{port['mkt_value']:>12,.2f}  (mixed; USD pos converted at ~84)")
    print(f"  TOTAL VALUE       : Rs{port['total']:>12,.2f}")
    sign = "+" if port["total_pnl"] >= 0 else ""
    print(f"  Total P&L         : Rs{sign}{port['total_pnl']:>11,.2f}  ({sign}{port['total_pct']:.2f}%)")
    print(f"  Open positions    : {port['n_positions']}")
    print(f"  Trades executed   : {port['n_trades']}")
    print()

    if port["positions"]:
        print("  OPEN POSITIONS:")
        for p in port["positions"]:
            sign = "+" if p["pnl"] >= 0 else ""
            print(f"    {p['symbol']:12}  qty={p['qty']:.0f}  "
                  f"avg=Rs{p['avg']:,.2f}  price=Rs{p['price']:,.2f}  "
                  f"P&L=Rs{sign}{p['pnl']:,.2f} ({sign}{p['pct']:.2f}%)")

    # ── Signal leaderboard ────────────────────────────────────────────────
    print(f"\n{DIVIDER}")
    print("  SIGNAL LEADERBOARD (sorted by score)")
    print(DIVIDER)
    sorted_sigs = sorted(all_signals, key=lambda x: x["score"], reverse=True)
    for s in sorted_sigs:
        n    = max(1, int(abs(s["score"]) * 30))
        bar  = (">" if s["score"] >= 0 else "<") * n
        tag  = {"buy": "BUY ", "sell": "SELL", "hold": "HOLD"}.get(s["action"], "HOLD")
        print(f"  {s['symbol']:12}  [{tag}]  {s['score']:+.4f}  |{bar:<30}|")

    print(f"\n{DIVIDER}")
    print(f"  Bought : {bought if bought else 'none'}")
    print(f"  Sold   : {sold if sold else 'none'}")
    print(f"  Held   : {len(held)} stocks")
    print(f"  Snapshot saved -> paper_trades.db")
    print(DIVIDER)
    print()

if __name__ == "__main__":
    paper_trading._init_db()
    run()
