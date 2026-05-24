"""
ML price-direction model  —  XGBoost classifier.

Feature set (13 features from daily OHLCV + indicators):
  ret_1d, ret_5d, ret_10d, rsi, rsi_slope, macd_hist, macd_hist_slope,
  macd_above_sig, bb_pct, vol_z, price_vs_sma20, price_vs_sma50, sma20_vs_sma50

Label: 5-day forward return
  > +1.5%  →  BUY  (class 2)
  < -1.5%  →  SELL (class 0)
  else     →  HOLD (class 1)

Output: score = P(BUY) - P(SELL) in [-1, +1]

Training:
  python -c "from analysis.ml_model import train; train()"
  Auto-retrain runs weekly the first time signals.py loads a stale model.
"""

import json
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

try:
    import joblib
    import xgboost as xgb
    from sklearn.model_selection import train_test_split
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False

import config
from analysis import technical as ta
from utils.logger import log

MODEL_PATH = Path(__file__).parent.parent / "ml_model.pkl"
META_PATH  = Path(__file__).parent.parent / "ml_model_meta.json"

FORWARD_DAYS     = 5
LABEL_THRESHOLD  = 0.015   # 1.5% forward return to label BUY/SELL
MIN_TRAIN_ROWS   = 300
RETRAIN_AGE_DAYS = 7

# Feature column names (order must match training)
FEATURE_COLS = [
    "ret_1d", "ret_5d", "ret_10d",
    "rsi", "rsi_slope",
    "macd_hist", "macd_hist_slope", "macd_above_sig",
    "bb_pct",
    "vol_z",
    "price_vs_sma20", "price_vs_sma50", "sma20_vs_sma50",
]


# ── Feature engineering ───────────────────────────────────────────────────────

def _features_from_df(df: pd.DataFrame) -> pd.DataFrame:
    """Compute ML feature matrix from raw OHLCV DataFrame. Returns rows with no NaN."""
    enriched = ta.enrich(df.copy())
    close = enriched["Close"]
    sma_s = f"SMA{config.SMA_SHORT}"
    sma_l = f"SMA{config.SMA_LONG}"

    feats = pd.DataFrame(index=enriched.index)

    # Price returns
    feats["ret_1d"]  = close.pct_change(1)
    feats["ret_5d"]  = close.pct_change(5)
    feats["ret_10d"] = close.pct_change(10)

    # RSI
    feats["rsi"]       = enriched["RSI"] / 100.0           # normalised 0-1
    feats["rsi_slope"] = feats["rsi"].diff(3)

    # MACD
    feats["macd_hist"]       = enriched["MACD_hist"]
    feats["macd_hist_slope"] = enriched["MACD_hist"].diff(2)
    feats["macd_above_sig"]  = (enriched["MACD"] > enriched["MACD_sig"]).astype(float)

    # Bollinger
    feats["bb_pct"] = enriched["BB_pct"].clip(0, 1)

    # Volume
    feats["vol_z"] = enriched["VOL_Z"].clip(-4, 4)

    # Trend
    sma20 = enriched[sma_s]
    sma50 = enriched[sma_l]
    feats["price_vs_sma20"] = (close - sma20) / sma20.replace(0, np.nan)
    feats["price_vs_sma50"] = (close - sma50) / sma50.replace(0, np.nan)
    feats["sma20_vs_sma50"] = (sma20 - sma50) / sma50.replace(0, np.nan)

    feats = feats[FEATURE_COLS].replace([np.inf, -np.inf], np.nan).dropna()
    return feats


def _labels_from_df(df: pd.DataFrame) -> pd.Series:
    """5-day forward-return labels: 2=BUY, 1=HOLD, 0=SELL. Last rows are NaN."""
    fwd = df["Close"].pct_change(FORWARD_DAYS).shift(-FORWARD_DAYS)
    lbl = pd.Series(1, index=df.index, dtype="Int64")
    lbl[fwd >  LABEL_THRESHOLD] = 2
    lbl[fwd < -LABEL_THRESHOLD] = 0
    lbl[fwd.isna()] = pd.NA
    return lbl


# ── Training ──────────────────────────────────────────────────────────────────

def train(symbols: list = None) -> bool:
    """
    Train XGBoost on 2 years of daily data from all watchlist symbols.
    Returns True on success and saves model + metadata to project root.
    """
    if not ML_AVAILABLE:
        log.warning("[ML] xgboost or scikit-learn not installed — skipping training")
        return False

    if symbols is None:
        symbols = config.WATCHLIST

    log.info("[ML] Collecting training data from %d symbols (2-year history)…", len(symbols))
    from data import stock_data

    all_X: list[pd.DataFrame] = []
    all_y: list[pd.Series]    = []

    for sym in symbols:
        try:
            df = stock_data.fetch_ohlcv(sym, period="2y", interval="1d")
            if df is None or len(df) < 60:
                continue
            X = _features_from_df(df)
            y = _labels_from_df(df).reindex(X.index).dropna()
            X = X.reindex(y.index)
            if len(X) < 50:
                continue
            all_X.append(X)
            all_y.append(y.astype(int))
            log.debug("[ML] %s: %d training rows", sym, len(X))
        except Exception as exc:
            log.warning("[ML] Skipping %s: %s", sym, exc)

    if not all_X:
        log.error("[ML] No training data collected — aborting")
        return False

    X_all = pd.concat(all_X)
    y_all = pd.concat(all_y)

    if len(X_all) < MIN_TRAIN_ROWS:
        log.warning("[ML] Only %d rows — need %d", len(X_all), MIN_TRAIN_ROWS)
        return False

    X_tr, X_te, y_tr, y_te = train_test_split(
        X_all, y_all, test_size=0.2, random_state=42, shuffle=False
    )

    model = xgb.XGBClassifier(
        n_estimators     = 400,
        max_depth        = 4,
        learning_rate    = 0.04,
        subsample        = 0.8,
        colsample_bytree = 0.8,
        min_child_weight = 5,
        eval_metric      = "mlogloss",
        random_state     = 42,
        n_jobs           = -1,
    )
    model.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], verbose=False)

    acc = float((model.predict(X_te) == y_te.values).mean())

    joblib.dump({"model": model, "feature_cols": FEATURE_COLS}, MODEL_PATH)
    meta = {
        "trained_at":   datetime.now().isoformat(),
        "n_samples":    int(len(X_all)),
        "n_symbols":    len(symbols),
        "feature_cols": FEATURE_COLS,
        "accuracy":     round(acc, 4),
        "n_classes":    3,
    }
    with open(META_PATH, "w") as f:
        json.dump(meta, f, indent=2)

    log.info("[ML] Training done — accuracy %.1f%% on %d held-out rows  (%d total samples)",
             acc * 100, len(X_te), len(X_all))
    return True


# ── Inference ─────────────────────────────────────────────────────────────────

def _load() -> tuple:
    """Return (model, feature_cols) or (None, None)."""
    if not MODEL_PATH.exists():
        return None, None
    try:
        data = joblib.load(MODEL_PATH)
        return data["model"], data["feature_cols"]
    except Exception as exc:
        log.warning("[ML] Could not load model: %s", exc)
        return None, None


def is_trained() -> bool:
    return MODEL_PATH.exists()


def retrain_if_stale(symbols: list = None) -> bool:
    """Retrain if model is missing or older than RETRAIN_AGE_DAYS. Returns True if training ran."""
    if not ML_AVAILABLE:
        return False
    if not META_PATH.exists():
        return train(symbols)
    try:
        with open(META_PATH) as f:
            meta = json.load(f)
        age = datetime.now() - datetime.fromisoformat(meta["trained_at"])
        if age > timedelta(days=RETRAIN_AGE_DAYS):
            log.info("[ML] Model is %d days old — retraining", age.days)
            return train(symbols)
    except Exception:
        return train(symbols)
    return False


def get_meta() -> dict:
    """Return training metadata, or empty dict."""
    if not META_PATH.exists():
        return {}
    try:
        with open(META_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


_NULL_RESULT = {
    "score":  0.0, "action": "hold",
    "p_buy":  0.333, "p_hold": 0.334, "p_sell": 0.333,
    "trained": False,
}


def predict(df: pd.DataFrame) -> dict:
    """
    Predict signal for the most recent bar of `df`.

    Returns dict with:
      score   — float [-1, +1]  (P(BUY) - P(SELL))
      action  — 'buy' | 'sell' | 'hold'
      p_buy, p_hold, p_sell  — class probabilities
      trained — bool
    """
    if not ML_AVAILABLE:
        return dict(_NULL_RESULT)

    model, feature_cols = _load()
    if model is None:
        return dict(_NULL_RESULT)

    try:
        X = _features_from_df(df)
        if X.empty:
            return dict(_NULL_RESULT)

        # Align feature columns (handle any schema drift)
        for col in feature_cols:
            if col not in X.columns:
                X[col] = 0.0
        row = X[feature_cols].iloc[[-1]]

        proba = model.predict_proba(row)[0]   # class order: 0=SELL, 1=HOLD, 2=BUY
        p_sell, p_hold, p_buy = float(proba[0]), float(proba[1]), float(proba[2])
        score = p_buy - p_sell

        return {
            "score":  round(score,  4),
            "action": "buy" if score > 0.15 else "sell" if score < -0.15 else "hold",
            "p_buy":  round(p_buy,  3),
            "p_hold": round(p_hold, 3),
            "p_sell": round(p_sell, 3),
            "trained": True,
        }
    except Exception as exc:
        log.warning("[ML] Prediction failed: %s", exc)
        return dict(_NULL_RESULT)
