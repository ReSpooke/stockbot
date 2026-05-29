#!/usr/bin/env python3
"""
train_intraday.py — train a GPU-accelerated XGBoost model on intraday features
to test whether the indicators have any *learnable* predictive edge.

Why this exists
───────────────
The hand-coded decision engine backtests to a losing −5% (profit factor 0.87).
That means either (a) the weights are wrong, or (b) the features themselves
carry no edge. ML answers that: if a model trained on the same features can
predict short-term moves out-of-sample (AUC > ~0.55), there is signal to
exploit. If AUC ≈ 0.50, the features are noise and no amount of tuning helps.

What it does
────────────
1. Loads the cached 60-day 5-min history (from backtest_cache/).
2. Builds a feature row at every bar: rsi, macd_hist, vol_ratio, dist-from-VWAP,
   OR-breakout flags, momentum, time-of-day.
3. Label = 1 if price rises > +0.3% over the next 6 bars (30 min), else 0.
4. Time-based split (train on older days, test on most recent) — no lookahead.
5. Trains XGBoost on GPU (device='cuda', falls back to CPU).
6. Reports out-of-sample accuracy, AUC, precision, and feature importance.
7. Saves model to ml_intraday.pkl.

Run:  python train_intraday.py [--universe all] [--horizon 6] [--target 0.003]
"""

import argparse
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import xgboost as xgb
from sklearn.metrics import roc_auc_score, accuracy_score, precision_score

from data.nse_stocks import NIFTY_50, ALL_SYMBOLS

CACHE_DIR = Path(__file__).parent / "backtest_cache"
MODEL_OUT = Path(__file__).parent / "ml_intraday.pkl"

FEATURES = ["pct_chg", "rsi", "macd_hist", "vol_ratio", "dist_vwap",
            "or_bull", "or_bear", "minute", "bar_idx"]


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def detect_device() -> str:
    """Return 'cuda' if XGBoost can use the GPU, else 'cpu'."""
    try:
        X = np.random.rand(64, 4)
        y = np.random.randint(0, 2, 64)
        xgb.XGBClassifier(device="cuda", tree_method="hist", n_estimators=2).fit(X, y)
        return "cuda"
    except Exception as e:
        _log(f"GPU not usable ({str(e)[:60]}…) — falling back to CPU")
        return "cpu"


def build_dataset(symbols, horizon: int, target: float):
    """Build feature matrix X and label vector y from cached intraday data."""
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    rows, labels, day_keys = [], [], []

    _log(f"Building features from {len(symbols)} stocks "
         f"(horizon={horizon} bars, target=+{target*100:.1f}%)…")

    loaded = 0
    for n, sym in enumerate(symbols, 1):
        cache = CACHE_DIR / f"{sym}.pkl"
        if not cache.exists():
            continue
        try:
            df = pickle.load(open(cache, "rb"))
        except Exception:
            continue
        loaded += 1

        for day in sorted(set(df.index.date)):
            bars = df[df.index.date == day]
            if len(bars) < 12:
                continue
            closes = bars["Close"].values
            highs  = bars["High"].values
            lows   = bars["Low"].values
            vols   = bars["Volume"].values

            prev = df[df.index.date < day]
            prev_close = float(prev["Close"].iloc[-1]) if not prev.empty else float(bars["Open"].iloc[0])
            prev_day_vol = None
            if not prev.empty:
                pdays = sorted(set(prev.index.date))
                prev_day_vol = float(prev[prev.index.date == pdays[-1]]["Volume"].sum())
            if not prev_day_vol or prev_day_vol <= 0:
                prev_day_vol = float(vols.mean()) * 75

            # Precompute series for the whole day (vectorised)
            s = pd.Series(closes)
            ema12 = s.ewm(span=12, adjust=False).mean()
            ema26 = s.ewm(span=26, adjust=False).mean()
            macd  = ema12 - ema26
            macd_hist = (macd - macd.ewm(span=9, adjust=False).mean()).values
            delta = s.diff()
            gain  = delta.clip(lower=0).rolling(14, min_periods=3).mean()
            loss  = (-delta.clip(upper=0)).rolling(14, min_periods=3).mean()
            rsi   = (100 - 100 / (1 + gain / loss.replace(0, np.nan))).fillna(50).values

            typ   = (highs + lows + closes) / 3
            cum_tpv = np.cumsum(typ * vols)
            cum_v   = np.cumsum(vols)
            vwap  = np.where(cum_v > 0, cum_tpv / np.maximum(cum_v, 1), closes)

            or_high = highs[:3].max()
            or_low  = lows[:3].min()

            for i in range(3, len(bars) - horizon):
                price = closes[i]
                if price <= 0:
                    continue
                # Forward label: did it rise > target over next `horizon` bars?
                future = closes[i + horizon]
                label  = 1 if (future - price) / price > target else 0

                bars_so_far = i + 1
                vol_ratio = vols[:i+1].sum() / max(prev_day_vol * (bars_so_far / 75), 1)
                t = bars.index[i]
                rows.append([
                    (price - prev_close) / prev_close * 100,    # pct_chg
                    rsi[i],                                     # rsi
                    macd_hist[i],                               # macd_hist
                    vol_ratio,                                  # vol_ratio
                    (price - vwap[i]) / vwap[i] * 100,          # dist_vwap
                    1 if price > or_high else 0,                # or_bull
                    1 if price < or_low else 0,                 # or_bear
                    t.hour * 60 + t.minute,                     # minute of day
                    bars_so_far,                                # bar_idx
                ])
                labels.append(label)
                day_keys.append(str(day))

        if n % 10 == 0:
            _log(f"  processed {n}/{len(symbols)} stocks, {len(rows):,} samples so far")

    X = np.array(rows, dtype=np.float32)
    y = np.array(labels, dtype=np.int32)
    _log(f"Dataset ready: {X.shape[0]:,} samples × {X.shape[1]} features "
         f"from {loaded} stocks. Positive rate: {y.mean()*100:.1f}%")
    return X, y, np.array(day_keys)


CACHE_LONG = Path(__file__).parent / "backtest_cache" / "hourly"
CACHE_LONG.mkdir(parents=True, exist_ok=True)


def _fetch_long(sym: str, period: str, interval: str):
    """Fetch + cache long-history bars (e.g. 2y of 1h) for one symbol."""
    import yfinance as yf
    import pytz
    cache = CACHE_LONG / f"{sym}_{interval}.pkl"
    if cache.exists() and (time.time() - cache.stat().st_mtime) < 86400:
        try:
            return pickle.load(open(cache, "rb"))
        except Exception:
            pass
    try:
        df = yf.Ticker(sym + ".NS").history(period=period, interval=interval, auto_adjust=True)
        if df is None or df.empty:
            return None
        df.index = df.index.tz_convert(pytz.timezone("Asia/Kolkata"))
        pickle.dump(df, open(cache, "wb"))
        return df
    except Exception:
        return None


def build_dataset_long(symbols, horizon: int, target: float, period: str, interval: str):
    """
    Build features from long history (e.g. 2 years of hourly bars).
    Indicators are computed CONTINUOUSLY across the whole series (hourly bars
    are too sparse for daily-reset RSI/MACD), with daily-reset VWAP + OR.
    """
    rows, labels, day_keys = [], [], []
    _log(f"Downloading {period} {interval} history for {len(symbols)} stocks "
         f"(cached after first run)…")

    loaded = 0
    for n, sym in enumerate(symbols, 1):
        df = _fetch_long(sym, period, interval)
        if df is None or len(df) < 200:
            continue
        loaded += 1

        c = df["Close"]
        # Continuous indicators across the full 2-year series
        ema12 = c.ewm(span=12, adjust=False).mean()
        ema26 = c.ewm(span=26, adjust=False).mean()
        macd  = ema12 - ema26
        macd_hist = (macd - macd.ewm(span=9, adjust=False).mean()).values
        delta = c.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rsi   = (100 - 100 / (1 + gain / loss.replace(0, np.nan))).fillna(50).values
        vol_avg = df["Volume"].rolling(20).mean().fillna(df["Volume"].mean()).values

        closes = c.values
        highs, lows, vols = df["High"].values, df["Low"].values, df["Volume"].values
        dates = df.index.date
        idxs  = df.index

        # Daily-reset VWAP + opening range, computed per day
        vwap = np.zeros(len(df)); or_high = np.full(len(df), np.nan); or_low = np.full(len(df), np.nan)
        prev_close_arr = np.zeros(len(df)); bar_idx_arr = np.zeros(len(df))
        cur_day = None; cum_tpv = 0.0; cum_v = 0.0; day_oh = -1e9; day_ol = 1e9; bi = 0
        last_close_prev_day = closes[0]
        for j in range(len(df)):
            if dates[j] != cur_day:
                if cur_day is not None:
                    last_close_prev_day = closes[j-1]
                cur_day = dates[j]; cum_tpv = 0.0; cum_v = 0.0
                day_oh = highs[j]; day_ol = lows[j]; bi = 0
            typ = (highs[j] + lows[j] + closes[j]) / 3
            cum_tpv += typ * vols[j]; cum_v += vols[j]
            vwap[j] = cum_tpv / cum_v if cum_v > 0 else closes[j]
            day_oh = max(day_oh, highs[j]); day_ol = min(day_ol, lows[j])
            or_high[j] = day_oh; or_low[j] = day_ol
            prev_close_arr[j] = last_close_prev_day
            bar_idx_arr[j] = bi; bi += 1

        for i in range(26, len(df) - horizon):
            price = closes[i]
            if price <= 0:
                continue
            future = closes[i + horizon]
            label  = 1 if (future - price) / price > target else 0
            pc = prev_close_arr[i] or price
            t  = idxs[i]
            rows.append([
                (price - pc) / pc * 100,                     # pct_chg
                rsi[i],                                      # rsi
                macd_hist[i],                                # macd_hist
                vols[i] / max(vol_avg[i], 1),                # vol_ratio
                (price - vwap[i]) / vwap[i] * 100,           # dist_vwap
                1 if price > or_high[i] else 0,              # or_bull
                1 if price < or_low[i] else 0,               # or_bear
                t.hour * 60 + t.minute,                      # minute
                bar_idx_arr[i],                              # bar_idx
            ])
            labels.append(label)
            day_keys.append(str(dates[i]))

        if n % 10 == 0:
            _log(f"  processed {n}/{len(symbols)} stocks, {len(rows):,} samples so far")

    X = np.array(rows, dtype=np.float32)
    y = np.array(labels, dtype=np.int32)
    _log(f"Dataset ready: {X.shape[0]:,} samples × {X.shape[1]} features "
         f"from {loaded} stocks ({period} {interval}). Positive rate: {y.mean()*100:.1f}%")
    return X, y, np.array(day_keys)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", choices=["nifty50", "all"], default="nifty50")
    ap.add_argument("--horizon", type=int,   default=6,     help="look-ahead bars")
    ap.add_argument("--target",  type=float, default=0.003, help="profit target fraction")
    ap.add_argument("--interval", default="5m", help="bar size: 5m (60d max) or 1h (2y)")
    ap.add_argument("--period",   default="", help="history window, e.g. 730d or 2y (for non-5m)")
    args = ap.parse_args()

    symbols = ALL_SYMBOLS if args.universe == "all" else NIFTY_50

    device = detect_device()
    _log(f"Training device: {device.upper()}")

    if args.interval == "5m":
        X, y, days = build_dataset(symbols, args.horizon, args.target)
    else:
        period = args.period or "730d"
        # For hourly, a 6-bar horizon ≈ a whole day; use a smaller default
        horizon = args.horizon if args.horizon != 6 else 2
        X, y, days = build_dataset_long(symbols, horizon, args.target, period, args.interval)
    if len(X) < 1000:
        _log("Not enough samples — run backtest.py first to populate the cache.")
        return

    # Time-based split: train on the oldest 70% of days, test on newest 30%
    uniq_days = sorted(set(days))
    cut_day   = uniq_days[int(len(uniq_days) * 0.7)]
    train_idx = days < cut_day
    test_idx  = days >= cut_day
    Xtr, ytr = X[train_idx], y[train_idx]
    Xte, yte = X[test_idx],  y[test_idx]
    _log(f"Train: {len(Xtr):,} samples (< {cut_day})  |  Test: {len(Xte):,} (>= {cut_day})")

    _log("Training XGBoost…")
    t0 = time.time()
    model = xgb.XGBClassifier(
        n_estimators   = 400,
        max_depth      = 5,
        learning_rate  = 0.05,
        subsample      = 0.8,
        colsample_bytree = 0.8,
        eval_metric    = "auc",
        device         = device,
        tree_method    = "hist",
    )
    model.fit(Xtr, ytr, eval_set=[(Xte, yte)], verbose=False)
    _log(f"Trained in {time.time()-t0:.1f}s")

    # ── Evaluate out-of-sample ──────────────────────────────────────────────
    proba = model.predict_proba(Xte)[:, 1]
    pred  = (proba >= 0.5).astype(int)
    auc   = roc_auc_score(yte, proba)
    acc   = accuracy_score(yte, pred)
    prec  = precision_score(yte, pred, zero_division=0)

    # Precision at high-confidence threshold (the trades we'd actually take)
    hi = proba >= 0.60
    prec_hi = precision_score(yte[hi], pred[hi], zero_division=0) if hi.sum() else 0
    base_rate = yte.mean()

    print()
    print("═" * 60)
    print("  INTRADAY ML MODEL — OUT-OF-SAMPLE RESULTS")
    print("═" * 60)
    print(f"  Test samples      : {len(yte):,}")
    print(f"  Base rate (up)    : {base_rate*100:.1f}%   ← random-guess accuracy")
    print(f"  Accuracy          : {acc*100:.1f}%")
    print(f"  AUC               : {auc:.4f}   ← 0.50 = no edge, >0.55 = real signal")
    print(f"  Precision @0.5    : {prec*100:.1f}%")
    print(f"  Precision @0.6    : {prec_hi*100:.1f}%   (on {hi.sum():,} high-confidence calls)")
    print("  " + "─" * 56)
    imp = sorted(zip(FEATURES, model.feature_importances_), key=lambda x: -x[1])
    print("  Feature importance:")
    for name, score in imp:
        bar = "█" * int(score * 50)
        print(f"    {name:<12} {score:.3f} {bar}")
    print("═" * 60)

    verdict = ("REAL EDGE — worth wiring into the bot" if auc >= 0.55
               else "WEAK/NO EDGE — features barely predict moves" if auc >= 0.52
               else "NO EDGE — these indicators don't predict intraday moves")
    print(f"  VERDICT: {verdict}  (AUC {auc:.3f})")
    print("═" * 60)

    out = MODEL_OUT if args.interval == "5m" else MODEL_OUT.with_name(f"ml_{args.interval}.pkl")
    pickle.dump({"model": model, "features": FEATURES,
                 "horizon": args.horizon, "target": args.target,
                 "interval": args.interval, "auc": auc, "device": device}, open(out, "wb"))
    _log(f"Model saved → {out.name}")


if __name__ == "__main__":
    main()
