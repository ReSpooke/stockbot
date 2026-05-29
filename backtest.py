#!/usr/bin/env python3
"""
backtest.py — replay the live intraday strategy against historical 5-min data.

It uses the EXACT same code the live bot uses:
  • analysis.indicators.compute_snapshot()  — indicator math
  • analysis.decision.evaluate()             — buy/sell decisions
so there is zero drift between backtest results and live behaviour.

Usage
-----
  python backtest.py                       # Nifty 50, ~60 days, default settings
  python backtest.py --universe all        # full NSE universe (~150 stocks)
  python backtest.py --days 30             # limit to last 30 trading days
  python backtest.py --buy-min 6           # test a stricter entry threshold
  python backtest.py --no-improvements     # disable re-entry guard + weak exit
  python backtest.py --capital 20000

Data
----
yfinance provides 5-min bars for roughly the last 60 days. Data is cached to
backtest_cache/ so repeat runs are instant.
"""

import argparse
import os
import pickle
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytz
import yfinance as yf

# Windows console: make ₹ and arrows printable
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import config
from analysis import decision
from analysis.indicators import compute_snapshot
from data.nse_stocks import NIFTY_50, ALL_SYMBOLS, get_name

_IST = pytz.timezone("Asia/Kolkata")
CACHE_DIR = Path(__file__).parent / "backtest_cache"
CACHE_DIR.mkdir(exist_ok=True)
MODEL_OUT = Path(__file__).parent / "ml_intraday.pkl"   # produced by train_intraday.py


# ══════════════════════════════════════════════════════════════════════════
#  Data loading (cached)
# ══════════════════════════════════════════════════════════════════════════

def _fetch_history(symbol: str) -> pd.DataFrame | None:
    """Fetch ~60 days of 5-min bars for one symbol, cached to disk."""
    cache = CACHE_DIR / f"{symbol}.pkl"
    if cache.exists() and (time.time() - cache.stat().st_mtime) < 86400:
        try:
            return pickle.load(open(cache, "rb"))
        except Exception:
            pass
    try:
        df = yf.Ticker(symbol + ".NS").history(period="60d", interval="5m", auto_adjust=True)
        if df is None or df.empty:
            return None
        df.index = df.index.tz_convert(_IST)
        pickle.dump(df, open(cache, "wb"))
        return df
    except Exception:
        return None


def load_universe(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Download (or load cached) history for every symbol. Returns {sym: df}."""
    data = {}
    print(f"Loading 60d 5-min history for {len(symbols)} stocks…")
    for i, sym in enumerate(symbols, 1):
        df = _fetch_history(sym)
        if df is not None and len(df) > 50:
            data[sym] = df
        print(f"\r  {i}/{len(symbols)}  ({len(data)} loaded)", end="", flush=True)
    print()
    return data


# ══════════════════════════════════════════════════════════════════════════
#  Backtest engine
# ══════════════════════════════════════════════════════════════════════════

class Portfolio:
    """Minimal intraday portfolio simulator mirroring trading/executor.py."""

    def __init__(self, capital: float, cost_bps: float = 0.0):
        self.cash       = capital
        self.initial    = capital
        self.positions  = {}            # sym → {qty, avg_cost}
        self.trades     = []            # list of dicts
        self.sold_at_loss_today = set() # for re-entry guard
        self.cost_rate  = cost_bps / 10000.0   # per-side cost as a fraction
        self.total_costs= 0.0

    def buy(self, sym, price, qty, reason, ts):
        gross = qty * price
        fee   = gross * self.cost_rate
        if qty <= 0 or gross + fee > self.cash:
            return False
        self.cash -= (gross + fee)
        self.total_costs += fee
        self.positions[sym] = {"qty": qty, "avg_cost": price}
        self.trades.append({"ts": ts, "side": "BUY", "symbol": sym,
                            "qty": qty, "price": price, "pnl": None, "reason": reason})
        return True

    def sell(self, sym, price, reason, ts):
        pos = self.positions.get(sym)
        if not pos:
            return 0.0
        qty   = pos["qty"]
        gross = qty * price
        fee   = gross * self.cost_rate
        # P&L is net of BOTH sides' costs (buy fee approximated on this notional)
        pnl   = (price - pos["avg_cost"]) * qty - fee - (qty * pos["avg_cost"] * self.cost_rate)
        self.cash += (gross - fee)
        self.total_costs += fee
        del self.positions[sym]
        if pnl < 0:
            self.sold_at_loss_today.add(sym)
        self.trades.append({"ts": ts, "side": "SELL", "symbol": sym,
                            "qty": qty, "price": price, "pnl": pnl, "reason": reason})
        return pnl

    def equity(self, prices: dict) -> float:
        mv = sum(p["qty"] * prices.get(s, p["avg_cost"]) for s, p in self.positions.items())
        return self.cash + mv

    def new_day(self):
        self.sold_at_loss_today.clear()


def _ml_features(snap: dict, hh: int, mm: int) -> list:
    """Build the feature vector the intraday model expects, from a snapshot."""
    price = snap["price"]; vwap = snap.get("vwap") or price
    orb   = snap.get("or_breakout")
    return [
        snap.get("pct_chg", 0),
        snap.get("rsi", 50),
        snap.get("macd_hist", 0),
        snap.get("vol_ratio", 1),
        (price - vwap) / vwap * 100 if vwap else 0,
        1 if orb == "bullish" else 0,
        1 if orb == "bearish" else 0,
        hh * 60 + mm,
        snap.get("bars", 0),
    ]


def run_backtest(data: dict, capital: float, buy_min: int,
                 use_improvements: bool, verbose: bool = True,
                 cost_bps: float = 0.0, strategy: str = "rules",
                 ml_bundle: dict | None = None, ml_threshold: float = 0.55) -> dict:
    """
    Replay every day in the loaded data through the decision engine.
    strategy: 'rules' = hand-coded decision.evaluate(); 'ml' = trained model.
    Returns a results dict with per-day P&L, trades, and summary stats.
    """
    decision.BUY_MIN_OVERRIDE = buy_min
    ml_model = ml_bundle["model"] if ml_bundle else None

    pf = Portfolio(capital, cost_bps=cost_bps)

    # Build the set of all trading days across the universe
    all_days = set()
    for df in data.values():
        all_days.update(pd.Series(df.index.date).unique())
    days = sorted(all_days)

    daily_pnl   = []   # (date, day_pnl, equity_after)
    equity_curve = [capital]

    if verbose:
        print(f"\n  Replaying {len(days)} trading days "
              f"(buy_min={buy_min}, improvements={'ON' if use_improvements else 'OFF'})…")
        print(f"  {'Date':<12} {'Trades':>7} {'Day P&L':>10} {'Equity':>12} {'Return':>9}")
        print("  " + "─" * 54)

    for day in days:
        # Slice each stock's bars for this day + previous day's close/volume
        day_bars = {}
        prev_meta = {}
        for sym, df in data.items():
            dts  = df.index.date
            today = df[dts == day]
            if len(today) < 5:
                continue
            prev = df[dts < day]
            prev_close = float(prev["Close"].iloc[-1]) if not prev.empty else None
            prev_vol   = None
            if not prev.empty:
                pdays = sorted(set(prev.index.date))
                prev_vol = float(prev[prev.index.date == pdays[-1]]["Volume"].sum())
            day_bars[sym]  = today
            prev_meta[sym] = (prev_close, prev_vol)

        if not day_bars:
            continue

        pf.new_day()
        day_start_equity = pf.equity({})

        # Determine the common timeline (use the longest stock's bar count)
        max_bars = max(len(b) for b in day_bars.values())

        # Step bar by bar (each bar = one 5-min scan cycle)
        for i in range(3, max_bars):
            # Build snapshots for every stock at this point in time
            snaps = {}
            for sym, bars in day_bars.items():
                if i >= len(bars):
                    continue
                window = bars.iloc[:i + 1]
                pc, pv = prev_meta[sym]
                snap = compute_snapshot(window, pc, pv)
                if snap:
                    snaps[sym] = snap

            if not snaps:
                continue

            ts   = day_bars[list(day_bars.keys())[0]].index[min(i, max_bars - 1)]
            hh, mm = ts.hour, ts.minute

            # Stop new entries / force square-off at SQUARE_OFF_TIME
            past_squareoff = (hh, mm) >= config.SQUARE_OFF_TIME

            # Batch-predict ML probabilities for every stock this bar (one call)
            ml_proba = {}
            if strategy == "ml" and snaps:
                syms_list = list(snaps.keys())
                feats = np.array([_ml_features(snaps[s], hh, mm) for s in syms_list],
                                 dtype=np.float32)
                probs = ml_model.predict_proba(feats)[:, 1]
                ml_proba = dict(zip(syms_list, probs))

            # ── Manage held positions (exits first) ──────────────────────
            for sym in list(pf.positions.keys()):
                snap = snaps.get(sym)
                if not snap:
                    continue
                price = snap["price"]
                avg   = pf.positions[sym]["avg_cost"]
                chg   = (price - avg) / avg

                # Hard stop-loss / take-profit
                if chg <= -config.STOP_LOSS_PCT:
                    pf.sell(sym, price, "stop-loss", ts);  continue
                if chg >= config.TAKE_PROFIT_PCT:
                    pf.sell(sym, price, "take-profit", ts); continue

                # Weak-position exit (improvement)
                if use_improvements and (hh, mm) >= config.WEAK_EXIT_TIME \
                   and snap.get("above_vwap") is False and chg < 0:
                    pf.sell(sym, price, "weak-exit", ts);  continue

                if strategy == "ml":
                    # Exit when the model no longer expects an up-move
                    if ml_proba.get(sym, 1.0) < 0.40:
                        pf.sell(sym, price, "ml-exit", ts)
                else:
                    dec = decision.evaluate(snap, hh, mm, holding=True, avg_cost=avg)
                    if dec["action"] == "SELL":
                        pf.sell(sym, price, dec.get("exit_type") or "signal", ts)

            # Square-off: close everything, skip new buys
            if past_squareoff:
                for sym in list(pf.positions.keys()):
                    if sym in snaps:
                        pf.sell(sym, snaps[sym]["price"], "square-off", ts)
                break   # day done

            # ── New entries ──────────────────────────────────────────────
            buys = []
            for sym, snap in snaps.items():
                if sym in pf.positions:
                    continue
                if use_improvements and config.NO_REENTRY_AFTER_LOSS \
                   and sym in pf.sold_at_loss_today:
                    continue
                if strategy == "ml":
                    p = float(ml_proba.get(sym, 0.0))
                    if p >= ml_threshold:
                        # rank by probability; size by conviction
                        qty_pct = 0.20 if p >= ml_threshold + 0.10 else 0.15
                        buys.append((p, qty_pct, sym, snap["price"]))
                else:
                    dec = decision.evaluate(snap, hh, mm, holding=False)
                    if dec["action"] == "BUY":
                        buys.append((dec["score"], dec["qty_pct"], sym, snap["price"]))

            buys.sort(reverse=True)   # highest score / probability first
            for score, qty_pct, sym, price in buys:
                if len(pf.positions) >= config.MAX_INTRADAY_POS:
                    break
                qty = max(1, int(pf.cash * qty_pct / price))
                if qty * price <= pf.cash * 0.95:
                    pf.buy(sym, price, qty, f"p={score:.2f}" if strategy == "ml" else f"score={score}", ts)

        # End of day — square off anything still open at last price
        for sym in list(pf.positions.keys()):
            last = day_bars[sym]["Close"].iloc[-1] if sym in day_bars else pf.positions[sym]["avg_cost"]
            pf.sell(sym, float(last), "eod", day_bars[sym].index[-1])

        day_equity = pf.equity({})
        day_pl     = day_equity - day_start_equity
        daily_pnl.append((day, day_pl, day_equity))
        equity_curve.append(day_equity)

        if verbose:
            trades_today = sum(1 for t in pf.trades
                               if hasattr(t["ts"], "date") and t["ts"].date() == day
                               and t["side"] == "SELL")
            ret = (day_equity - capital) / capital * 100
            mark = "▲" if day_pl >= 0 else "▼"
            print(f"  {str(day):<12} {trades_today:>7} {mark}₹{day_pl:>8.2f} "
                  f"₹{day_equity:>10.2f} {ret:>+7.2f}%", flush=True)

    decision.BUY_MIN_OVERRIDE = None   # reset
    return _summarise(pf, daily_pnl, equity_curve, capital)


# ══════════════════════════════════════════════════════════════════════════
#  Reporting
# ══════════════════════════════════════════════════════════════════════════

def _summarise(pf: Portfolio, daily_pnl: list, equity_curve: list, capital: float) -> dict:
    sells = [t for t in pf.trades if t["side"] == "SELL" and t["pnl"] is not None]
    wins  = [t for t in sells if t["pnl"] > 0]
    losses= [t for t in sells if t["pnl"] <= 0]

    gross_win  = sum(t["pnl"] for t in wins)
    gross_loss = sum(t["pnl"] for t in losses)

    # Max drawdown on the daily equity curve
    peak = equity_curve[0]; max_dd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        max_dd = min(max_dd, (e - peak) / peak)

    final = equity_curve[-1]
    winning_days = sum(1 for _, p, _ in daily_pnl if p > 0)

    # Per-symbol P&L
    sym_pnl = defaultdict(float)
    for t in sells:
        sym_pnl[t["symbol"]] += t["pnl"]

    return {
        "final_equity":  final,
        "total_return":  (final - capital) / capital * 100,
        "n_days":        len(daily_pnl),
        "winning_days":  winning_days,
        "n_trades":      len(sells),
        "n_wins":        len(wins),
        "n_losses":      len(losses),
        "win_rate":      len(wins) / len(sells) * 100 if sells else 0,
        "gross_win":     gross_win,
        "gross_loss":    gross_loss,
        "profit_factor": (gross_win / abs(gross_loss)) if gross_loss else float("inf"),
        "avg_win":       gross_win / len(wins) if wins else 0,
        "avg_loss":      gross_loss / len(losses) if losses else 0,
        "max_drawdown":  max_dd * 100,
        "best_day":      max((p for _, p, _ in daily_pnl), default=0),
        "worst_day":     min((p for _, p, _ in daily_pnl), default=0),
        "daily_pnl":     daily_pnl,
        "sym_pnl":       dict(sym_pnl),
        "trades":        pf.trades,
    }


def print_report(r: dict, label: str, capital: float):
    print()
    print("═" * 64)
    print(f"  BACKTEST RESULT — {label}")
    print("═" * 64)
    ret = r["total_return"]
    arrow = "▲" if ret >= 0 else "▼"
    print(f"  Starting capital : ₹{capital:>12,.2f}")
    print(f"  Final equity     : ₹{r['final_equity']:>12,.2f}   {arrow} {ret:+.2f}%")
    print(f"  Trading days     : {r['n_days']}   (profitable: {r['winning_days']})")
    print("  " + "─" * 60)
    print(f"  Total trades     : {r['n_trades']}")
    print(f"  Win rate         : {r['win_rate']:.1f}%   ({r['n_wins']}W / {r['n_losses']}L)")
    print(f"  Profit factor    : {r['profit_factor']:.2f}   (gross win ÷ gross loss)")
    print(f"  Avg win / loss   : +₹{r['avg_win']:.2f} / ₹{r['avg_loss']:.2f}")
    print(f"  Max drawdown     : {r['max_drawdown']:.2f}%")
    print(f"  Best / worst day : +₹{r['best_day']:.2f} / ₹{r['worst_day']:.2f}")
    print("  " + "─" * 60)

    # Top 5 / bottom 5 symbols
    syms = sorted(r["sym_pnl"].items(), key=lambda x: x[1], reverse=True)
    if syms:
        print("  Best symbols     : " + ", ".join(f"{s} +₹{p:.0f}" for s, p in syms[:5] if p > 0))
        worst = [x for x in syms if x[1] < 0][-5:]
        if worst:
            print("  Worst symbols    : " + ", ".join(f"{s} ₹{p:.0f}" for s, p in worst))
    print("═" * 64)


# ══════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Backtest the StockBot intraday strategy")
    ap.add_argument("--universe", choices=["nifty50", "all"], default="nifty50")
    ap.add_argument("--capital",  type=float, default=config.INITIAL_CAPITAL)
    ap.add_argument("--buy-min",  type=int,   default=5, help="entry score threshold")
    ap.add_argument("--days",     type=int,   default=0, help="limit to last N trading days (0=all)")
    ap.add_argument("--no-improvements", action="store_true",
                    help="disable re-entry guard + weak-position exit")
    ap.add_argument("--ab", action="store_true",
                    help="run A/B: baseline vs improvements, side by side")
    ap.add_argument("--cost-bps", type=float, default=10.0,
                    help="round-trip trading cost in basis points (10 = 0.10%)")
    ap.add_argument("--strategy", choices=["rules", "ml", "compare"], default="rules",
                    help="'compare' = rules vs ML head-to-head")
    ap.add_argument("--ml-threshold", type=float, default=0.55,
                    help="ML probability threshold to buy")
    args = ap.parse_args()

    symbols = ALL_SYMBOLS if args.universe == "all" else NIFTY_50
    data = load_universe(symbols)
    if not data:
        print("No data loaded — yfinance may be rate-limited. Try again shortly.")
        return

    # Optionally trim to last N days
    if args.days > 0:
        all_days = sorted({d for df in data.values() for d in pd.Series(df.index.date).unique()})
        keep = set(all_days[-args.days:])
        data = {s: df[pd.Series(df.index.date, index=df.index).isin(keep)] for s, df in data.items()}

    # Load ML model if needed
    ml_bundle = None
    if args.strategy in ("ml", "compare"):
        if not MODEL_OUT.exists():
            print(f"No model at {MODEL_OUT.name}. Run train_intraday.py first.")
            return
        ml_bundle = pickle.load(open(MODEL_OUT, "rb"))
        try:
            ml_bundle["model"].set_params(device="cpu")   # fast single-batch CPU predict
        except Exception:
            pass
        print(f"Loaded ML model (trained AUC {ml_bundle.get('auc', 0):.3f})")

    print(f"\nTrading cost model: {args.cost_bps:.0f} bps round-trip "
          f"(₹{args.cost_bps/100:.2f} per ₹100 traded)")

    if args.ab:
        print("\nRunning A/B comparison (baseline vs improvements)…")
        base = run_backtest(data, args.capital, args.buy_min, use_improvements=False,
                            verbose=False, cost_bps=args.cost_bps)
        impr = run_backtest(data, args.capital, args.buy_min, use_improvements=True,
                            verbose=False, cost_bps=args.cost_bps)
        print_report(base, f"BASELINE (buy_min={args.buy_min}, no improvements)", args.capital)
        print_report(impr, "WITH IMPROVEMENTS (re-entry guard + weak exit)", args.capital)
        print(f"\n  Δ return : {impr['total_return'] - base['total_return']:+.2f} pts")

    elif args.strategy == "compare":
        print("\nRunning RULES vs ML head-to-head (with costs)…")
        rules = run_backtest(data, args.capital, args.buy_min, use_improvements=True,
                             verbose=False, cost_bps=args.cost_bps, strategy="rules")
        ml    = run_backtest(data, args.capital, args.buy_min, use_improvements=True,
                             verbose=False, cost_bps=args.cost_bps, strategy="ml",
                             ml_bundle=ml_bundle, ml_threshold=args.ml_threshold)
        print_report(rules, "RULES engine (hand-coded weights)", args.capital)
        print_report(ml,    f"ML model (XGBoost, p≥{args.ml_threshold})", args.capital)
        print()
        print(f"  Rules return : {rules['total_return']:+.2f}%")
        print(f"  ML return    : {ml['total_return']:+.2f}%")
        print(f"  Winner       : {'ML' if ml['total_return'] > rules['total_return'] else 'RULES'}")

    else:
        r = run_backtest(data, args.capital, args.buy_min,
                         use_improvements=not args.no_improvements,
                         cost_bps=args.cost_bps, strategy=args.strategy,
                         ml_bundle=ml_bundle, ml_threshold=args.ml_threshold)
        print_report(r, f"{args.universe}, {args.strategy}", args.capital)


if __name__ == "__main__":
    main()
