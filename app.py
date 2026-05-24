"""
Flask web application — StockBot dashboard.

Run:  python app.py
Then open: http://localhost:5000
"""

import threading
import time
from datetime import datetime

from flask import Flask, jsonify, render_template, redirect, request, url_for

import config
from database import db
from data import stock_data, news_scraper
from analysis import signals as sig_gen
from analysis import sentiment as sent
from trading import portfolio as pf
from trading import executor
from utils.logger import log

app = Flask(__name__)
app.secret_key = config.WEB_SECRET

# ── Shared analysis state (updated by background thread) ─────────────────

_state = {
    "running":       False,
    "last_run":      None,
    "last_signals":  [],
    "status_msg":    "Idle — click Run Analysis to start",
    "current_symbol":"",
    "progress":      0,         # 0-100
    "total_symbols": len(config.WATCHLIST),
}
_lock = threading.Lock()

# ── Live data caches (updated by background threads) ─────────────────────

_prices_cache: dict = {}          # symbol → float
_prices_ts:    str  = ""          # last updated timestamp
_prices_lock   = threading.Lock()

_news_cache: dict = {}            # symbol → list[article]
_news_ts:    str  = ""
_news_lock   = threading.Lock()

NEWS_REFRESH_SECS   = 60          # refresh news every 60 s
PRICES_REFRESH_SECS = 120         # refresh prices every 2 min


def _background_news_refresh() -> None:
    """Continuously refresh news for all watchlist symbols every 60 seconds."""
    while True:
        for sym in config.WATCHLIST:
            try:
                articles = news_scraper.fetch_news(sym)
                for a in articles:
                    a["sentiment"] = sent.score_headline(a.get("title", ""))
                with _news_lock:
                    _news_cache[sym] = articles
            except Exception as exc:
                log.debug("News refresh failed for %s: %s", sym, exc)
        with _news_lock:
            global _news_ts
            _news_ts = datetime.now().strftime("%H:%M:%S")
        log.info("News cache refreshed for %d symbols", len(config.WATCHLIST))
        time.sleep(NEWS_REFRESH_SECS)


def _background_price_refresh() -> None:
    """Continuously refresh live prices every 2 minutes."""
    while True:
        try:
            fresh = stock_data.get_batch_prices(config.WATCHLIST)
            with _prices_lock:
                _prices_cache.update(fresh)
                global _prices_ts
                _prices_ts = datetime.now().strftime("%H:%M:%S")
            log.debug("Price cache refreshed: %d symbols", len(fresh))
        except Exception as exc:
            log.debug("Price refresh failed: %s", exc)
        time.sleep(PRICES_REFRESH_SECS)


def _start_background_threads() -> None:
    for fn in (_background_news_refresh, _background_price_refresh):
        t = threading.Thread(target=fn, daemon=True)
        t.start()


def _maybe_train_ml() -> None:
    """Train ML model in background if missing. Runs once at startup."""
    from analysis import ml_model
    if not ml_model.is_trained():
        log.info("[ML] Model not found — training on startup (takes ~2 min)…")
        ml_model.train()
        log.info("[ML] Startup training complete")


# ── HTML page ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template(
        "index.html",
        watchlist  = config.WATCHLIST,
        mode       = config.TRADING_MODE,
        market     = config.MARKET,
        initial_cap= config.INITIAL_CAPITAL,
    )


# ── Zerodha OAuth ──────────────────────────────────────────────────────────

@app.route("/zerodha/login")
def zerodha_login():
    from trading import zerodha
    try:
        url = zerodha.get_login_url()
        return redirect(url)
    except RuntimeError as exc:
        return f"<h2>Error</h2><p>{exc}</p><p>Set ZERODHA_API_KEY in your .env file.</p>", 400


@app.route("/zerodha/callback")
def zerodha_callback():
    request_token = request.args.get("request_token")
    if not request_token:
        return redirect("/?auth=failed")
    from trading import zerodha
    token = zerodha.complete_login(request_token)
    if token:
        return redirect("/?auth=ok")
    return redirect("/?auth=failed")


# ── JSON APIs ──────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    with _lock:
        return jsonify(dict(_state))


@app.route("/api/portfolio")
def api_portfolio():
    prices = stock_data.get_batch_prices(config.WATCHLIST)
    summary = pf.summary(prices)
    # attach currency symbol
    summary["currency"] = "₹" if config.MARKET == "NSE" else "$"
    return jsonify(summary)


@app.route("/api/signals")
def api_signals():
    rows = db.get_recent_signals(len(config.WATCHLIST) * 2)
    # one per symbol (latest)
    seen = set()
    out  = []
    for r in rows:
        if r["symbol"] not in seen:
            seen.add(r["symbol"])
            out.append(r)
    return jsonify(out)


@app.route("/api/news")
def api_news():
    symbol = request.args.get("symbol")
    return jsonify(db.get_recent_news(symbol, limit=40))


@app.route("/api/trades")
def api_trades():
    return jsonify(db.get_trades(30))


@app.route("/api/watchlist")
def api_watchlist():
    with _prices_lock:
        cached = dict(_prices_cache)
    # fill any gaps with a fresh fetch
    missing = [s for s in config.WATCHLIST if s not in cached]
    if missing:
        fresh = stock_data.get_batch_prices(missing)
        cached.update(fresh)
    return jsonify([
        {
            "symbol":   s,
            "price":    cached.get(s),
            "market":   config.SYMBOL_MARKET.get(s, config.MARKET),
            "currency": "₹" if config.SYMBOL_MARKET.get(s, config.MARKET) == "NSE" else "$",
        }
        for s in config.WATCHLIST
    ])


@app.route("/api/zerodha/status")
def api_zerodha_status():
    from trading import zerodha
    auth = zerodha.is_authenticated()
    user = zerodha.get_user_info() if auth else {}
    return jsonify({"authenticated": auth, "user": user})


@app.route("/api/market_status")
def api_market_status():
    all_status = stock_data.get_all_market_status()
    return jsonify({
        "markets":     all_status,
        "primary":     config.MARKET,
        "primary_open": stock_data.is_market_open(config.MARKET),
    })


@app.route("/api/live_prices")
def api_live_prices():
    with _prices_lock:
        return jsonify({
            "prices":     dict(_prices_cache),
            "updated_at": _prices_ts,
            "symbol_market": config.SYMBOL_MARKET,
        })


@app.route("/api/live_news")
def api_live_news():
    symbol = request.args.get("symbol")
    with _news_lock:
        if symbol:
            articles = _news_cache.get(symbol, [])
        else:
            articles = []
            for arts in _news_cache.values():
                articles.extend(arts)
            articles.sort(key=lambda a: a.get("published_at", ""), reverse=True)
            articles = articles[:60]
        return jsonify({
            "articles":   articles,
            "updated_at": _news_ts,
        })


# ── Analysis trigger ───────────────────────────────────────────────────────

@app.route("/api/analyse", methods=["POST"])
def api_analyse():
    with _lock:
        if _state["running"]:
            return jsonify({"status": "already_running"})
        _state["running"]       = True
        _state["status_msg"]    = "Starting analysis…"
        _state["progress"]      = 0
        _state["current_symbol"]= ""
        _state["last_signals"]  = []

    symbols = request.json.get("symbols") if request.is_json else None
    symbols = symbols or config.WATCHLIST

    thread = threading.Thread(target=_run_analysis_bg, args=(symbols,), daemon=True)
    thread.start()
    return jsonify({"status": "started", "symbols": symbols})


def _run_analysis_bg(symbols: list) -> None:
    signals = []
    total   = len(symbols)
    try:
        for i, sym in enumerate(symbols):
            with _lock:
                _state["current_symbol"] = sym
                _state["status_msg"]     = f"Analysing {sym} ({i+1}/{total})…"
                _state["progress"]       = int((i / total) * 100)

            df = stock_data.fetch_ohlcv(sym)
            if df is None or len(df) < 30:
                log.warning("[%s] Skipped — insufficient data", sym)
                continue

            articles = news_scraper.fetch_news(sym)
            for a in articles:
                a["sentiment"] = sent.score_headline(
                    f"{a.get('title','')}. {a.get('summary','')}"
                )
            db.save_news_batch(sym, articles)

            signal = sig_gen.generate(sym, df, articles)
            db.save_signal(signal)
            signals.append(signal)

        # Execute trades based on signals
        prices = {s["symbol"]: s.get("close") for s in signals if s.get("close")}
        executor.check_stop_loss_take_profit(prices)
        for s in signals:
            sym    = s["symbol"]
            action = s["action"]
            price  = s.get("close")
            if price is None:
                continue
            if action == "buy":
                executor.buy(sym, price, reason="; ".join(s.get("reasons", [])[:3]))
            elif action == "sell":
                executor.sell(sym, price, reason="; ".join(s.get("reasons", [])[:3]))

    except Exception as exc:
        log.error("Analysis thread error: %s", exc)
    finally:
        with _lock:
            _state["running"]        = False
            _state["progress"]       = 100
            _state["last_run"]       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _state["last_signals"]   = signals
            _state["current_symbol"] = ""
            _state["status_msg"]     = f"Done — {len(signals)} signals generated at {_state['last_run']}"


# ── Manual trade ───────────────────────────────────────────────────────────

@app.route("/api/trade", methods=["POST"])
def api_trade():
    data   = request.get_json() or {}
    symbol = data.get("symbol", "").upper()
    side   = data.get("side", "").lower()
    price  = data.get("price")

    if not symbol or side not in ("buy", "sell"):
        return jsonify({"ok": False, "message": "Invalid symbol or side"}), 400

    if price is None:
        price = stock_data.get_current_price(symbol)
    if price is None:
        return jsonify({"ok": False, "message": f"Cannot get price for {symbol}"}), 400

    result = executor.buy(symbol, price, "Manual trade") \
             if side == "buy" \
             else executor.sell(symbol, price, "Manual trade")

    return jsonify(result)


# ── Entry point ────────────────────────────────────────────────────────────

def create_app():
    db.init_db()
    _start_background_threads()
    threading.Thread(target=_maybe_train_ml, daemon=True).start()
    return app


if __name__ == "__main__":
    db.init_db()
    _start_background_threads()
    threading.Thread(target=_maybe_train_ml, daemon=True).start()
    log.info("Starting StockBot web server at http://%s:%d", config.WEB_HOST, config.WEB_PORT)
    app.run(
        host  = config.WEB_HOST,
        port  = config.WEB_PORT,
        debug = config.WEB_DEBUG,
        use_reloader=False,    # prevent double-init of background threads
    )
