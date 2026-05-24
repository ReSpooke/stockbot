"""
Trading signal generator — v3 (ML + regime, 2026-05-24).

Eight sub-scores combined into a single [-1, +1] signal:

  RSI            (15%) — momentum oscillator
  MACD           (25%) — leading trend crossover
  SMA Crossover  (10%) — lagging trend direction
  Bollinger Band ( 8%) — mean-reversion proximity
  Volume         ( 4%) — conviction behind price move
  Momentum       ( 8%) — 5-day price return vs 20-day baseline
  Sentiment      (12%) — aggregated news sentiment
  ML Model       (18%) — XGBoost P(BUY) - P(SELL) over 13 features
                          [falls back to 0 if model not trained]

Thresholds are adjusted per market regime (bull/bear/sideways/volatile).
Train ML model:  python -c "from analysis.ml_model import train; train()"
"""

import numpy as np

import config
from analysis import technical as ta
from analysis import sentiment as sent
from analysis import ml_model, regime as reg
from utils.logger import log

# Weights when ML model is available
_WEIGHTS_WITH_ML = {
    "rsi":           0.15,
    "macd":          0.25,
    "sma_crossover": 0.10,
    "bollinger":     0.08,
    "volume":        0.04,
    "momentum":      0.08,
    "sentiment":     0.12,
    "ml":            0.18,
}
# Weights when ML model is not trained (redistributes ML weight)
_WEIGHTS_NO_ML = {
    "rsi":           0.20,
    "macd":          0.30,
    "sma_crossover": 0.12,
    "bollinger":     0.10,
    "volume":        0.05,
    "momentum":      0.08,
    "sentiment":     0.15,
    "ml":            0.00,
}


# ── Sub-scorers ───────────────────────────────────────────────────────────────

def _rsi_score(rsi):
    if rsi is None:
        return 0.0, []
    if rsi <= config.RSI_OVERSOLD:
        return 1.0, [f"RSI {rsi:.1f} oversold (buy zone <=30)"]
    if rsi >= config.RSI_OVERBOUGHT:
        return -1.0, [f"RSI {rsi:.1f} overbought (sell zone >=70)"]
    if rsi < 50:
        score = (50 - rsi) / (50 - config.RSI_OVERSOLD)
    else:
        score = -(rsi - 50) / (config.RSI_OVERBOUGHT - 50)
    return float(np.clip(score, -1, 1)), [f"RSI {rsi:.1f} (neutral range)"]


def _macd_score(snap):
    macd      = snap.get("macd")
    macd_sig  = snap.get("macd_signal")
    hist      = snap.get("macd_hist")
    hist_prev = snap.get("macd_hist_prev", 0)
    macd_prev = snap.get("macd_prev", 0)
    sig_prev  = snap.get("macd_sig_prev", 0)

    if any(v is None for v in (macd, macd_sig, hist)):
        return 0.0, []

    score   = 0.0
    reasons = []

    if macd_prev <= sig_prev and macd > macd_sig:
        score += 1.0
        reasons.append("MACD bullish crossover — momentum turning up")
    elif macd_prev >= sig_prev and macd < macd_sig:
        score -= 1.0
        reasons.append("MACD bearish crossover — momentum turning down")
    elif macd > macd_sig:
        score += 0.5
        reasons.append(f"MACD above signal line ({macd:.2f} > {macd_sig:.2f})")
    else:
        score -= 0.5
        reasons.append(f"MACD below signal line ({macd:.2f} < {macd_sig:.2f})")

    if hist > hist_prev:
        score = min(score + 0.3, 1.0)
        reasons.append("MACD histogram expanding (accelerating)")
    elif hist < hist_prev:
        score = max(score - 0.3, -1.0)
        reasons.append("MACD histogram shrinking (decelerating)")

    return float(np.clip(score, -1, 1)), reasons


def _sma_score(snap):
    close   = snap.get("close")
    sma_s   = snap.get("sma_short")
    sma_l   = snap.get("sma_long")
    sma_s_p = snap.get("sma_short_prev", 0)
    sma_l_p = snap.get("sma_long_prev", 0)

    if any(v is None for v in (close, sma_s, sma_l)):
        return 0.0, []

    reasons = []
    if sma_s_p <= sma_l_p and sma_s > sma_l:
        score = 1.0
        reasons.append(f"Golden cross: SMA{config.SMA_SHORT} crossed above SMA{config.SMA_LONG}")
    elif sma_s_p >= sma_l_p and sma_s < sma_l:
        score = -1.0
        reasons.append(f"Death cross: SMA{config.SMA_SHORT} crossed below SMA{config.SMA_LONG}")
    elif sma_s > sma_l:
        score = 0.6
        reasons.append(f"Uptrend: SMA{config.SMA_SHORT} ({sma_s:.1f}) > SMA{config.SMA_LONG} ({sma_l:.1f})")
    else:
        score = -0.6
        reasons.append(f"Downtrend: SMA{config.SMA_SHORT} ({sma_s:.1f}) < SMA{config.SMA_LONG} ({sma_l:.1f})")

    if close > sma_s:
        score = min(score + 0.2, 1.0)
        reasons.append(f"Price {close:.1f} above SMA{config.SMA_SHORT}")
    else:
        score = max(score - 0.2, -1.0)
        reasons.append(f"Price {close:.1f} below SMA{config.SMA_SHORT}")

    return float(np.clip(score, -1, 1)), reasons


def _bollinger_score(snap):
    pct = snap.get("bb_pct")
    if pct is None:
        return 0.0, []
    if pct < 0.10:
        return 0.8,  [f"Near lower Bollinger band (%B={pct:.2f}) — potential bounce"]
    if pct > 0.90:
        return -0.6, [f"Near upper Bollinger band (%B={pct:.2f}) — potential reversal"]
    if pct < 0.30:
        return 0.3,  [f"Below Bollinger midline (%B={pct:.2f})"]
    if pct > 0.70:
        return -0.2, [f"Above Bollinger midline (%B={pct:.2f})"]
    return 0.0, [f"Mid Bollinger Band (%B={pct:.2f})"]


def _volume_score(snap):
    vol_z = snap.get("vol_z")
    close = snap.get("close")
    sma_s = snap.get("sma_short")
    if vol_z is None or abs(vol_z) < 1.0:
        return 0.0, ["Volume within normal range"]
    price_up = close is not None and sma_s is not None and close > sma_s
    score = 0.5 if (vol_z > 0 and price_up) else (-0.5 if vol_z > 0 else 0.0)
    label = "rising" if price_up else "falling"
    return float(np.clip(score, -1, 1)), [f"Volume spike (Z={vol_z:.1f}) on {label} price"]


def _momentum_score(df):
    """
    5-day price return relative to 20-day average daily return.
    Positive → short-term momentum above average → bullish.
    Negative → short-term momentum below average → bearish.
    """
    try:
        close = df["Close"]
        if len(close) < 25:
            return 0.0, []
        ret_5d  = (close.iloc[-1] / close.iloc[-6] - 1) * 100  # 5-day return %
        ret_20d = (close.pct_change().tail(20).mean()) * 100    # avg daily return %
        baseline = ret_20d * 5  # expected 5d return if avg holds

        diff = ret_5d - baseline
        # scale: ±5% excess is ±1.0
        score = float(np.clip(diff / 5.0, -1, 1))
        direction = "above" if diff > 0 else "below"
        return score, [f"5d return {ret_5d:+.2f}% is {abs(diff):.2f}pp {direction} 20d baseline"]
    except Exception:
        return 0.0, []


# ── Main generator ────────────────────────────────────────────────────────────

def generate(symbol: str, df, articles: list) -> dict:
    snap = ta.latest_snapshot(df)
    if not snap:
        log.warning("[%s] No indicator data — returning hold", symbol)
        return {"symbol": symbol, "action": "hold", "score": 0.0, "reasons": ["No data"]}

    # ── Regime detection ─────────────────────────────────────────────────────
    regime_info  = reg.detect(df)
    buy_thr, sell_thr = reg.adjusted_thresholds(regime_info)

    # ── Technical sub-scores ─────────────────────────────────────────────────
    rsi_s,  rsi_r  = _rsi_score(snap.get("rsi"))
    macd_s, macd_r = _macd_score(snap)
    sma_s,  sma_r  = _sma_score(snap)
    bb_s,   bb_r   = _bollinger_score(snap)
    vol_s,  vol_r  = _volume_score(snap)
    mom_s,  mom_r  = _momentum_score(df)

    sentiment_avg = sent.aggregate_sentiment(articles)
    sent_s = float(np.clip(sentiment_avg, -1, 1))
    sent_r = [f"News: {sent.label(sentiment_avg)} ({sentiment_avg:+.2f}, {len(articles)} articles)"]

    # ── ML sub-score ─────────────────────────────────────────────────────────
    ml_result = ml_model.predict(df)
    ml_s      = float(np.clip(ml_result["score"], -1, 1))
    ml_trained = ml_result.get("trained", False)
    ml_r = []
    if ml_trained:
        ml_r = [
            f"ML: P(buy)={ml_result['p_buy']:.2f} P(hold)={ml_result['p_hold']:.2f} "
            f"P(sell)={ml_result['p_sell']:.2f} → {ml_result['action'].upper()}"
        ]

    # ── Weighted combination ──────────────────────────────────────────────────
    w = _WEIGHTS_WITH_ML if ml_trained else _WEIGHTS_NO_ML

    combined = (
        w["rsi"]           * rsi_s  +
        w["macd"]          * macd_s +
        w["sma_crossover"] * sma_s  +
        w["bollinger"]     * bb_s   +
        w["volume"]        * vol_s  +
        w["momentum"]      * mom_s  +
        w["sentiment"]     * sent_s +
        w["ml"]            * ml_s
    )
    combined = float(np.clip(combined, -1, 1))

    if combined >= buy_thr:
        action = "buy"
    elif combined <= sell_thr:
        action = "sell"
    else:
        action = "hold"

    log.info(
        "[%s] %s  score=%.3f  RSI=%.1f  MACD=%.3f  ML=%.3f  regime=%s  thr=%.2f/%.2f",
        symbol, action.upper(), combined,
        snap.get("rsi") or 0, macd_s, ml_s,
        regime_info["regime"], buy_thr, sell_thr,
    )

    return {
        "symbol":           symbol,
        "action":           action,
        "score":            round(combined, 4),
        "reasons":          rsi_r + macd_r + sma_r + bb_r + vol_r + mom_r + sent_r + ml_r,
        "rsi_score":        round(rsi_s,  4),
        "macd_score":       round(macd_s, 4),
        "sma_score":        round(sma_s,  4),
        "bb_score":         round(bb_s,   4),
        "volume_score":     round(vol_s,  4),
        "momentum_score":   round(mom_s,  4),
        "sentiment_score":  round(sent_s, 4),
        "ml_score":         round(ml_s,   4),
        "ml_p_buy":         round(ml_result.get("p_buy",  0.333), 3),
        "ml_p_sell":        round(ml_result.get("p_sell", 0.333), 3),
        "ml_trained":       ml_trained,
        "regime":           regime_info["regime"],
        "regime_trend_pct": regime_info.get("trend_pct", 0.0),
        "regime_vol_ann":   regime_info.get("vol_ann",   0.0),
        "buy_threshold":    round(buy_thr,  3),
        "sell_threshold":   round(sell_thr, 3),
        "rsi_value":        snap.get("rsi"),
        "sentiment_avg":    round(sentiment_avg, 4),
        "close":            snap.get("close"),
    }
