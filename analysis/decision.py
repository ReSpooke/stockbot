"""
decision.py — multi-factor intraday trading brain.

evaluate(snap, ist_hour, ist_minute) → {
    action:     "BUY" | "SELL" | "HOLD"
    score:      int   (−13 … +13 theoretical range)
    confidence: "HIGH" | "MEDIUM" | "LOW"
    qty_pct:    float (fraction of available cash to use)
    reasons:    list[str]
}

Seven independent factors with explicit point values so every decision
is fully explainable.  No black-box ML here — pure rule-based confluence.

Factors
───────
1. Opening Range Breakout (±3)  — first 15 min high/low, most reliable
2. VWAP position        (±2)  — institutional price consensus
3. RSI zone             (±2)  — momentum strength + overbought guard
4. MACD histogram       (±2)  — trend direction and momentum
5. Volume               (±2)  — conviction filter
6. Price momentum       (±1)  — direction sanity check
7. Time-of-day         (0/−2 or hard-block)  — avoid bad timing windows

BUY  when total score ≥ BUY_MIN  (default 5)
SELL when total score ≤ SELL_MIN (default −4)
"""

BUY_MIN  =  5
SELL_MIN = -4


def evaluate(snap: dict, ist_hour: int = 10, ist_minute: int = 0) -> dict:
    """
    Score a single intraday snapshot and return a trade decision.

    snap keys used: price, pct_chg, rsi, macd_hist, vol_ratio,
                    above_vwap, or_breakout, vwap
    """
    if not snap or not snap.get("price"):
        return _result("HOLD", 0, ["No snapshot data"])

    score   = 0
    reasons = []

    price   = snap.get("price", 0)
    pct     = snap.get("pct_chg", 0) or 0
    rsi     = snap.get("rsi", 50) or 50
    mhist   = snap.get("macd_hist", 0) or 0
    vol_r   = snap.get("vol_ratio", 1) or 1
    orb     = snap.get("or_breakout", "inside")

    # Compute above_vwap if not already in snapshot
    above_v = snap.get("above_vwap")
    if above_v is None:
        vwap = snap.get("vwap")
        above_v = (price > vwap) if vwap else None

    # ── Hard block: first 5 minutes are too noisy ─────────────────────────
    if (ist_hour, ist_minute) < (9, 20):
        return _result("HOLD", 0, ["Too early — skip first 5 min"])

    # ── 1. Opening Range Breakout ─────────────────────────────────────────
    if orb == "bullish":
        score += 3
        reasons.append("OR breakout ↑ (+3)")
    elif orb == "bearish":
        score -= 3
        reasons.append("OR breakdown ↓ (−3)")
    # "inside" = price within first-15-min range, neutral

    # ── 2. VWAP position ─────────────────────────────────────────────────
    if above_v is True:
        score += 2
        reasons.append("Above VWAP (+2)")
    elif above_v is False:
        score -= 2
        reasons.append("Below VWAP (−2)")

    # ── 3. RSI zone ──────────────────────────────────────────────────────
    if 38 <= rsi <= 55:
        # Sweet spot: not oversold, not overbought, healthy uptrend
        score += 2
        reasons.append(f"RSI {rsi:.0f} buy-zone (+2)")
    elif 55 < rsi <= 65:
        score += 1
        reasons.append(f"RSI {rsi:.0f} momentum (+1)")
    elif rsi < 38:
        # Oversold — potential bounce, but wait for other confirmation
        score += 1
        reasons.append(f"RSI {rsi:.0f} oversold (+1)")
    elif rsi > 72:
        score -= 2
        reasons.append(f"RSI {rsi:.0f} overbought (−2)")
    elif rsi > 65:
        score -= 1
        reasons.append(f"RSI {rsi:.0f} extended (−1)")

    # ── 4. MACD histogram ────────────────────────────────────────────────
    if mhist > 0.08:
        score += 2
        reasons.append(f"MACD strong +{mhist:.3f} (+2)")
    elif mhist > 0:
        score += 1
        reasons.append(f"MACD positive (+1)")
    elif mhist < -0.08:
        score -= 2
        reasons.append(f"MACD weak {mhist:.3f} (−2)")
    elif mhist < 0:
        score -= 1
        reasons.append("MACD negative (−1)")

    # ── 5. Volume ────────────────────────────────────────────────────────
    if vol_r >= 3.0:
        score += 2
        reasons.append(f"Volume surge {vol_r:.1f}× (+2)")
    elif vol_r >= 1.5:
        score += 1
        reasons.append(f"Vol above-avg {vol_r:.1f}× (+1)")
    elif vol_r < 0.6:
        # Suspiciously low volume — avoid
        score -= 1
        reasons.append(f"Low volume {vol_r:.1f}× (−1)")

    # ── 6. Price momentum ────────────────────────────────────────────────
    if 0.3 <= pct <= 2.5:
        score += 1
        reasons.append(f"+{pct:.1f}% healthy move (+1)")
    elif pct > 2.5:
        # Already run too far — late entry risk
        score -= 1
        reasons.append(f"+{pct:.1f}% possibly exhausted (−1)")
    elif pct <= -0.5:
        score -= 1
        reasons.append(f"{pct:.1f}% negative momentum (−1)")

    # ── 7. Time-of-day ───────────────────────────────────────────────────
    if (ist_hour, ist_minute) >= (14, 30):
        # New buys after 2:30 PM don't have enough time to recover
        score -= 2
        reasons.append("Late entry >14:30 (−2)")

    # ── Decision ─────────────────────────────────────────────────────────
    if score >= BUY_MIN:
        return _result("BUY", score, reasons)
    if score <= SELL_MIN:
        return _result("SELL", score, reasons)
    return _result("HOLD", score, reasons)


def _result(action: str, score: int, reasons: list) -> dict:
    qty_pct = 0.0
    if action == "BUY":
        if score >= 9:
            qty_pct = 0.20
        elif score >= 7:
            qty_pct = 0.16
        else:
            qty_pct = 0.12

    abs_s = abs(score)
    confidence = "HIGH" if abs_s >= 8 else "MEDIUM" if abs_s >= 6 else "LOW"

    return {
        "action":     action,
        "score":      score,
        "confidence": confidence,
        "qty_pct":    qty_pct,
        "reasons":    reasons,
    }
