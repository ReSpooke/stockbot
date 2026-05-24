"""
Multi-source financial news scraper — supports both Indian (NSE) and US markets.

Free sources (no API key):
  - yfinance news endpoint
  - Yahoo Finance RSS
  - Google News RSS
  - Moneycontrol RSS      (India)
  - Economic Times RSS    (India)
  - Business Standard RSS (India)
  - Finviz news table     (US)
  - MarketWatch RSS       (US)
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import quote_plus

import feedparser
import requests
import yfinance as yf
from bs4 import BeautifulSoup

import config
from utils.logger import log

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}
_TIMEOUT = 12

# Company-name aliases used for relevance filtering of broad RSS feeds.
# If an article title/summary contains ANY of these strings (case-insensitive),
# it counts as relevant to the symbol.  The ticker itself is always included.
_COMPANY_ALIASES: dict[str, list[str]] = {
    "RELIANCE":    ["reliance industries", "ril ", "mukesh ambani", "jio", "reliance retail"],
    "TCS":         ["tata consultancy", "tcs ", "tata cs"],
    "INFY":        ["infosys", "infy", "narayana murthy"],
    "HDFCBANK":    ["hdfc bank", "hdfc ", "hdfcbank"],
    "ICICIBANK":   ["icici bank", "icici ", "icicibank"],
    "SBIN":        ["state bank", "sbi ", "sbin"],
    "WIPRO":       ["wipro"],
    "HINDUNILVR":  ["hindustan unilever", "hul ", "hindunilvr", "unilever india"],
    "ITC":         ["itc limited", "itc ltd", " itc "],
    "BAJFINANCE":  ["bajaj finance", "bajfinance", "bajaj fin"],
    "HCLTECH":     ["hcl tech", "hcltech", "hcl technologies"],
    "ADANIENT":    ["adani enterprises", "adani group", "adanient", "gautam adani"],
    # US stocks
    "AAPL":        ["apple inc", "apple stock", "tim cook", "iphone"],
    "MSFT":        ["microsoft", "satya nadella", "azure"],
    "GOOGL":       ["alphabet", "google", "sundar pichai"],
    "TSLA":        ["tesla", "elon musk", "electric vehicle"],
    "NVDA":        ["nvidia", "jensen huang", "gpu"],
    "META":        ["meta platforms", "facebook", "zuckerberg", "instagram"],
}


def _is_relevant(symbol: str, text: str) -> bool:
    """Return True if *text* mentions the company by ticker or any known alias."""
    text_lower = text.lower()
    if symbol.lower() in text_lower:
        return True
    for alias in _COMPANY_ALIASES.get(symbol, []):
        if alias.lower() in text_lower:
            return True
    return False


# ── Public ───────────────────────────────────────────────────────────────────

def fetch_news(symbol: str) -> list:
    market = config.SYMBOL_MARKET.get(symbol, config.MARKET)
    if market == "NSE":
        scrapers = [_yfinance_news, _yahoo_rss, _google_news_rss,
                    _moneycontrol_rss, _et_markets_rss, _business_standard_rss]
    else:
        scrapers = [_yfinance_news, _yahoo_rss, _google_news_rss,
                    _finviz_news, _marketwatch_rss]

    articles = []
    with ThreadPoolExecutor(max_workers=len(scrapers)) as pool:
        futures = {pool.submit(fn, symbol): fn.__name__ for fn in scrapers}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                result = fut.result()
                articles.extend(result)
            except Exception as exc:
                log.debug("[%s] %s failed: %s", symbol, name, exc)

    articles = _deduplicate(articles)
    articles = _filter_by_recency(articles, config.NEWS_LOOKBACK_HOURS)
    articles = articles[: config.MAX_NEWS_PER_SYMBOL]
    log.info("[%s] %d news articles collected", symbol, len(articles))
    return articles


# ── Individual scrapers ───────────────────────────────────────────────────────

def _yfinance_news(symbol: str) -> list:
    market = config.SYMBOL_MARKET.get(symbol, config.MARKET)
    yf_sym = symbol + ".NS" if market == "NSE" else symbol
    ticker = yf.Ticker(yf_sym)
    out = []
    for item in (ticker.news or []):
        content = item.get("content", {})
        title   = content.get("title") or item.get("title", "")
        if not title:
            continue
        pub_ts = content.get("pubDate") or item.get("providerPublishTime")
        out.append({
            "title":        title.strip(),
            "source":       content.get("provider", {}).get("displayName", "Yahoo Finance"),
            "url":          content.get("canonicalUrl", {}).get("url") or item.get("link", ""),
            "published_at": _parse_ts(pub_ts),
            "summary":      content.get("summary", ""),
        })
    return out


def _yahoo_rss(symbol: str) -> list:
    market = config.SYMBOL_MARKET.get(symbol, config.MARKET)
    if market == "NSE":
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}.NS&region=IN&lang=en-IN"
    else:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
    return _parse_rss(url, "Yahoo Finance")


def _google_news_rss(symbol: str) -> list:
    # Use first alias as the search query — more precise than ticker symbol
    aliases = _COMPANY_ALIASES.get(symbol, [symbol])
    query   = (aliases[0] if aliases else symbol) + " NSE stock"
    url     = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-IN&gl=IN&ceid=IN:en"
    articles = _parse_rss(url, "Google News")
    # Relevance-filter: only keep articles that actually mention the company
    relevant = [a for a in articles
                if _is_relevant(symbol, a["title"] + " " + (a.get("summary") or ""))]
    return relevant  # empty list OK — better than irrelevant noise


def _moneycontrol_rss(symbol: str) -> list:
    urls = [
        "https://www.moneycontrol.com/rss/MCrecentnews.xml",
        "https://www.moneycontrol.com/rss/marketoutlook.xml",
    ]
    articles = []
    for url in urls:
        articles.extend(_parse_rss(url, "Moneycontrol"))
    relevant = [a for a in articles
                if _is_relevant(symbol, a["title"] + " " + (a.get("summary") or ""))]
    # Only fall back to general market news if ZERO relevant articles found
    return relevant if relevant else []


def _et_markets_rss(symbol: str) -> list:
    url      = "https://economictimes.indiatimes.com/markets/rss.cms"
    articles = _parse_rss(url, "Economic Times")
    return [a for a in articles
            if _is_relevant(symbol, a["title"] + " " + (a.get("summary") or ""))]


def _business_standard_rss(symbol: str) -> list:
    url      = "https://www.business-standard.com/rss/markets-106.rss"
    articles = _parse_rss(url, "Business Standard")
    return [a for a in articles
            if _is_relevant(symbol, a["title"] + " " + (a.get("summary") or ""))]


def _finviz_news(symbol: str) -> list:
    url = f"https://finviz.com/quote.ashx?t={symbol}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
    except Exception:
        return []
    soup = BeautifulSoup(resp.text, "lxml")
    table = soup.find("table", id="news-table")
    if not table:
        return []
    articles = []
    current_date = ""
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        date_cell = cells[0].text.strip()
        if len(date_cell) > 8:
            parts = date_cell.split()
            current_date = parts[0] if len(parts) >= 2 else ""
            time_str = parts[-1] if len(parts) >= 2 else date_cell
        else:
            time_str = date_cell
        link_tag = cells[1].find("a")
        if not link_tag:
            continue
        articles.append({
            "title":        link_tag.text.strip(),
            "source":       "Finviz",
            "url":          link_tag.get("href", ""),
            "published_at": f"{current_date} {time_str}".strip(),
            "summary":      "",
        })
    return articles


def _marketwatch_rss(symbol: str) -> list:
    url = "https://feeds.marketwatch.com/marketwatch/realtimeheadlines/"
    articles = _parse_rss(url, "MarketWatch")
    sym_lower = symbol.lower()
    return [a for a in articles if sym_lower in a["title"].lower()]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_rss(url: str, default_source: str) -> list:
    try:
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries:
            title = getattr(entry, "title", "").strip()
            if not title:
                continue
            pub_parsed = getattr(entry, "published_parsed", None)
            pub_str    = _parse_ts(pub_parsed) if pub_parsed else ""
            articles.append({
                "title":        title,
                "source":       default_source,
                "url":          getattr(entry, "link", ""),
                "published_at": pub_str,
                "summary":      _strip_html(getattr(entry, "summary", "")),
            })
        return articles
    except Exception as exc:
        log.debug("RSS parse failed for %s: %s", url, exc)
        return []


def _parse_ts(ts) -> str:
    if ts is None:
        return ""
    if isinstance(ts, (int, float)):
        try:
            return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""
    if isinstance(ts, time.struct_time):
        try:
            return datetime(*ts[:6]).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""
    return str(ts)[:19]


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _deduplicate(articles: list) -> list:
    seen: set = set()
    unique = []
    for a in articles:
        key = re.sub(r"\W+", "", a["title"].lower())[:80]
        if key and key not in seen:
            seen.add(key)
            unique.append(a)
    return unique


def _filter_by_recency(articles: list, hours: int) -> list:
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    filtered = []
    for a in articles:
        pub = a.get("published_at", "")
        if not pub:
            filtered.append(a)
            continue
        try:
            dt = datetime.strptime(pub[:19], "%Y-%m-%d %H:%M:%S")
            if dt >= cutoff:
                filtered.append(a)
        except Exception:
            filtered.append(a)
    return filtered
