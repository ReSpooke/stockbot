"""
decision.py — multi-factor intraday trading brain.

evaluate(snap, ist_hour, ist_minute, holding, avg_cost) → {
    action:     "BUY" | "SELL" | "HOLD"
    score:      int
    confidence: "HIGH" | "MEDIUM" | "LOW"
    qty_pct:    float (fraction of cash to deploy)
    reasons:    list[str]
    exit_type:  str | None  (why we're selling: "signal" | "profit_book" | "momentum_exit")
}

Two modes
─────────
NOT holding → buy logic only (score ≥ 5 = BUY)
Holding     → exit logic is primary; sell threshold is −3 (lower than −4 for new
              shorts) plus extra triggers specific to open positions:
              • Overbought after gain      (RSI > 68 when up >1%)   → sell to lock
              • VWAP breakdown             (price fell below VWAP)  → momentum exit
              • After-hours profit booking (after 14:30, close +ve) → take profit early
              • After-hours stop           (after 14:30, any loss)  → cut fast

Seven scoring factors (identical for buy and sell side):
  1. OR breakout / breakdown   (±3)
  2. VWAP position             (±2)
  3. RSI zone                  (±2)
  4. MACD histogram            (±2)
  5. Volume                    (±2)
  6. Price momentum            (±1)
  7. Time-of-day               (penalty / hard-block)
"""

SELL_MIN_NEW   = -4    # min score to exit (no position held)
SELL_MIN_HELD  = -3    # lower threshold when we hold the stock (exit faster)

def _buy_min() -> int:
    """Dynamic BUY threshold adjusted by the learning module."""
    try:
        from analysis.learning import get_buy_min
        return get_buy_min()
    except Exception:
        return 5


def evaluate(
    snap:       dict,
    ist_hour:   int  = 10,
    ist_minute: int  = 0,
    holding:    bool = False,
    avg_cost:   float | None = None,
) -> dict:
    """
    Score a stock and decide whether to BUY / SELL / HOLD.

    Parameters
    ----------
    snap      : intraday snapshot dict (price, pct_chg, rsi, macd_hist,
                vol_ratio, above_vwap, or_breakout, vwap)
    ist_hour  : current IST hour   (used for time filters)
    ist_minute: current IST minute
    holding   : True if we currently hold a position in this stock
    avg_cost  : our entry price (used to compute unrealised P&L for exits)
    """
    if not snap or not snap.get("price"):
        return _result("HOLD", 0, ["No data"], None)

    price   = float(snap.get("price", 0))
    pct     = float(snap.get("pct_chg", 0) or 0)
    rsi     = float(snap.get("rsi", 50) or 50)
    mhist   = float(snap.get("macd_hist", 0) or 0)
    vol_r   = float(snap.get("vol_ratio", 1) or 1)
    orb     = snap.get("or_breakout", "inside")
    above_v = snap.get("above_vwap")
    if above_v is None:
        vwap = snap.get("vwap")
        above_v = (price > vwap) if vwap else None

    now = (ist_hour, ist_minute)

    # ── Hard block: first 5 min ───────────────────────────────────────────
    if now < (9, 20):
        return _result("HOLD", 0, ["Too early — skip first 5 min"], None)

    # ─────────────────────────────────────────────────────────────────────
    # HOLDING-SPECIFIC EXIT CHECKS  (run before general scoring)
    # These override the score model for positions we already own.
    # ─────────────────────────────────────────────────────────────────────
    if holding and avg_cost and price > 0:
        unrealised_pct = (price - avg_cost) / avg_cost * 100

        # 1. Overbought after a gain → lock profit before reversal
        if rsi > 68 and unrealised_pct > 1.0:
            return _result("SELL", -10, [
                f"RSI {rsi:.0f} overbought with +{unrealised_pct:.1f}% gain → lock profit"
            ], "profit_lock")

        # 2. VWAP breakdown from above → momentum has reversed
        if above_v is False and unrealised_pct > 0:
            return _result("SELL", -8, [
                f"Fell below VWAP with +{unrealised_pct:.1f}% gain → momentum exit"
            ], "momentum_exit")

        # 3. After 14:30 with any profit → book early, not worth the risk
        if now >= (14, 30) and unrealised_pct > 0.5:
            return _result("SELL", -9, [
                f"+{unrealised_pct:.1f}% profit at {ist_hour}:{ist_minute:02d} — booking before close"
            ], "profit_book")

        # 4. After 14:30 with a loss → cut it fast, don't hold into square-off
        if now >= (14, 30) and unrealised_pct < -0.5:
            return _result("SELL", -9, [
                f"{unrealised_pct:.1f}% loss at {ist_hour}:{ist_minute:02d} — cutting before EOD"
            ], "loss_cut")

        # 5. MACD turns strongly negative while we're down → exit now
        if mhist < -0.1 and above_v is False and unrealised_pct < -0.5:
            return _result("SELL", -9, [
                f"MACD weak + below VWAP + {unrealised_pct:.1f}% → stop"
            ], "momentum_exit")

    # ─────────────────────────────────────────────────────────────────────
    # GENERAL SCORING  (applies to both buy candidates and held exits)
    # ─────────────────────────────────────────────────────────────────────
    score   = 0
    reasons = []

    # 1. Opening Range Breakout
    if orb == "bullish":
        score += 3;  reasons.append("OR breakout ↑ (+3)")
    elif orb == "bearish":
        score -= 3;  reasons.append("OR breakdown ↓ (−3)")

    # 2. VWAP
    if above_v is True:
        score += 2;  reasons.append("Above VWAP (+2)")
    elif above_v is False:
        score -= 2;  reasons.append("Below VWAP (−2)")

    # 3. RSI
    if 38 <= rsi <= 55:
        score += 2;  reasons.append(f"RSI {rsi:.0f} buy-zone (+2)")
    elif 55 < rsi <= 65:
        score += 1;  reasons.append(f"RSI {rsi:.0f} momentum (+1)")
    elif rsi < 38:
        score += 1;  reasons.append(f"RSI {rsi:.0f} oversold (+1)")
    elif rsi > 72:
        score -= 2;  reasons.append(f"RSI {rsi:.0f} overbought (−2)")
    elif rsi > 65:
        score -= 1;  reasons.append(f"RSI {rsi:.0f} extended (−1)")

    # 4. MACD histogram
    if mhist > 0.08:
        score += 2;  reasons.append(f"MACD strong +{mhist:.3f} (+2)")
    elif mhist > 0:
        score += 1;  reasons.append("MACD positive (+1)")
    elif mhist < -0.08:
        score -= 2;  reasons.append(f"MACD weak {mhist:.3f} (−2)")
    elif mhist < 0:
        score -= 1;  reasons.append("MACD negative (−1)")

    # 5. Volume
    if vol_r >= 3.0:
        score += 2;  reasons.append(f"Volume surge {vol_r:.1f}× (+2)")
    elif vol_r >= 1.5:
        score += 1;  reasons.append(f"Vol above-avg {vol_r:.1f}× (+1)")
    elif vol_r < 0.6:
        score -= 1;  reasons.append(f"Low volume {vol_r:.1f}× (−1)")

    # 6. Price momentum
    if 0.3 <= pct <= 2.5:
        score += 1;  reasons.append(f"+{pct:.1f}% healthy move (+1)")
    elif pct > 2.5:
        score -= 1;  reasons.append(f"+{pct:.1f}% possibly exhausted (−1)")
    elif pct <= -0.5:
        score -= 1;  reasons.append(f"{pct:.1f}% negative momentum (−1)")

    # 7. Time-of-day
    if now >= (14, 30):
        score -= 2;  reasons.append("Late entry >14:30 (−2)")

    # ── Decision ─────────────────────────────────────────────────────────
    sell_min = SELL_MIN_HELD if holding else SELL_MIN_NEW
    buy_min  = _buy_min()

    if score >= buy_min and not holding:
        return _result("BUY", score, reasons, None)
    if score <= sell_min:
        return _result("SELL", score, reasons, "signal")
    return _result("HOLD", score, reasons, None)


def _result(action: str, score: int, reasons: list, exit_type) -> dict:
    qty_pct = 0.0
    if action == "BUY":
        qty_pct = 0.20 if score >= 9 else 0.16 if score >= 7 else 0.12

    abs_s = abs(score)
    confidence = "HIGH" if abs_s >= 8 else "MEDIUM" if abs_s >= 6 else "LOW"

    return {
        "action":     action,
        "score":      score,
        "confidence": confidence,
        "qty_pct":    qty_pct,
        "reasons":    reasons,
        "exit_type":  exit_type,
    }
