"""
Sentiment analysis for financial news headlines.

Uses VADER (Valence Aware Dictionary and sEntiment Reasoner) enhanced with
a custom financial lexicon so that domain-specific terms like "plunge",
"surge", "earnings beat", etc. are scored appropriately.

VADER returns a compound score in [-1.0, +1.0]:
  +1.0 = maximally positive
   0.0 = neutral
  -1.0 = maximally negative
"""

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# Custom financial-domain sentiment boosters/suppressors
# Positive values → bullish signal; negative → bearish.
_FINANCIAL_LEXICON: dict[str, float] = {
    # strongly bullish
    "surge":        2.5,
    "soar":         2.5,
    "skyrocket":    3.0,
    "rally":        2.0,
    "boom":         2.0,
    "breakout":     2.0,
    "record":       1.5,
    "outperform":   2.0,
    "upgrade":      2.0,
    "beat":         1.5,
    "bullish":      2.0,
    "buyback":      1.5,
    "acquisition":  1.2,
    "partnership":  1.0,
    "dividend":     1.0,
    "ipo":          1.0,
    "profit":       1.2,
    "revenue":      0.8,
    "growth":       1.0,
    "expansion":    1.0,
    "recovery":     1.5,
    "rebound":      1.5,
    "exceed":       1.5,
    # strongly bearish
    "plunge":      -2.5,
    "crash":       -3.0,
    "collapse":    -3.0,
    "tumble":      -2.0,
    "selloff":     -2.5,
    "sell-off":    -2.5,
    "downgrade":   -2.0,
    "miss":        -1.5,
    "loss":        -1.5,
    "losses":      -1.5,
    "layoffs":     -2.0,
    "bankruptcy":  -3.5,
    "recession":   -2.5,
    "fraud":       -3.0,
    "scandal":     -2.5,
    "investigation": -2.0,
    "lawsuit":     -1.5,
    "recall":      -2.0,
    "bearish":     -2.0,
    "decline":     -1.2,
    "drop":        -1.0,
    "fall":        -0.8,
    "warning":     -1.5,
    "concern":     -1.0,
    "debt":        -0.5,
    "default":     -3.0,
    "cut":         -1.0,   # dividend cut, rate cut (ambiguous but usually bad for stock)
    "volatile":    -0.5,
}

# Lazy-init the analyser (loading the lexicon is slightly expensive)
_analyser: SentimentIntensityAnalyzer | None = None


def _get_analyser() -> SentimentIntensityAnalyzer:
    global _analyser
    if _analyser is None:
        _analyser = SentimentIntensityAnalyzer()
        _analyser.lexicon.update(_FINANCIAL_LEXICON)
    return _analyser


def score_headline(text: str) -> float:
    """
    Return the VADER compound score for *text* in [-1.0, +1.0].
    Returns 0.0 if text is empty.
    """
    if not text or not text.strip():
        return 0.0
    return _get_analyser().polarity_scores(text)["compound"]


def aggregate_sentiment(articles: list[dict]) -> float:
    """
    Compute a single sentiment score for a list of article dicts.

    Each dict must have a "title" key; "summary" is used as additional signal
    if present.  Returns the mean compound score (0.0 if no articles).
    """
    if not articles:
        return 0.0

    scores = []
    for a in articles:
        title   = a.get("title", "")
        summary = a.get("summary", "") or ""
        combined = f"{title}. {summary}".strip(". ")
        scores.append(score_headline(combined))

    return sum(scores) / len(scores)


def label(score: float) -> str:
    """Human-readable sentiment label."""
    if score >=  0.35:
        return "BULLISH"
    if score <= -0.35:
        return "BEARISH"
    return "NEUTRAL"
