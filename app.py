"""
Flask web application — StockBot intraday trading dashboard.

Run:  python app.py
Then open: http://localhost:5000
"""

import os
import threading
import time
import urllib.request
from datetime import datetime

import pytz
from flask import Flask, jsonify, render_template, redirect, request, url_for

import config
from database import db
from data import stock_data, news_scraper
from data.nse_stocks import screen_top, search as nse_search
from analysis import signals as sig_gen
from analysis import sentiment as sent
from analysis.intraday import intraday_snapshot, intraday_signal
from trading import portfolio as pf
from trading import executor
from utils.logger import log

app = Flask(__name__)
app.secret_key = config.WEB_SECRET

_IST = pytz.timezone("Asia/Kolkata")

# ── Shared analysis state (updated by background thread) ─────────────────

_state = {
    "running":       False,
    "last_run":      None,
    "last_signals":  [],
    "status_msg":    "Idle — click Run Analysis to start",
    "current_symbol":"",
    "progress":      0,
    "total_symbols": len(config.WATCHLIST),
}
_lock = threading.Lock()

# ── Intraday auto-trading state ───────────────────────────────────────────

_intraday = {
    "enabled":       False,     # toggled by user via /api/auto_trade
    "last_scan":     None,      # ISO timestamp of last 5-min scan
    "scan_results":  [],        # top stocks from last screener run
    "daily_pnl":     0.0,       # realised P&L today (₹)
    "trades_today":  0,
    "squared_off":   False,     # True after EOD square-off
    "scan_status":   "idle",    # idle | scanning | done
}
_intraday_lock = threading.Lock()

# ── Live data caches (updated by background threads) ─────────────────────

_prices_cache: dict = {}
_prices_ts:    str  = ""
_prices_lock   = threading.Lock()

_news_cache: dict = {}
_news_ts:    str  = ""
_news_lock   = threading.Lock()

NEWS_REFRESH_SECS   = 60
PRICES_REFRESH_SECS = 120


def _ist_now():
    return datetime.now(tz=_IST)


def _nse_market_open() -> bool:
    """True during actual NSE session: 9:15 AM – 3:30 PM IST, Mon–Fri."""
    now = _ist_now()
    if now.weekday() >= 5:
        return False
    t = (now.hour, now.minute)
    return (9, 15) <= t <= (15, 30)


def _is_trading_hours() -> bool:
    """True when the bot may scan and place trades: 9:15 AM – 3:15 PM IST."""
    now = _ist_now()
    if now.weekday() >= 5:
        return False
    t = (now.hour, now.minute)
    return (9, 15) <= t <= config.SQUARE_OFF_TIME


def _background_news_refresh() -> None:
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
            _news_ts = datetime.now().strftime("%H:%M:%S")
        log.info("News cache refreshed for %d symbols", len(config.WATCHLIST))
        time.sleep(NEWS_REFRESH_SECS)


def _background_price_refresh() -> None:
    while True:
        try:
            fresh = stock_data.get_batch_prices(config.WATCHLIST)
            with _prices_lock:
                _prices_cache.update(fresh)
                _prices_ts = datetime.now().strftime("%H:%M:%S")
        except Exception as exc:
            log.debug("Price refresh failed: %s", exc)
        time.sleep(PRICES_REFRESH_SECS)


def _background_intraday_scan() -> None:
    """Every SCAN_INTERVAL_SECS: screen top stocks + auto-trade if enabled."""
    while True:
        if not _is_trading_hours():
            time.sleep(30)
            continue

        with _intraday_lock:
            enabled     = _intraday["enabled"]
            squared_off = _intraday["squared_off"]
            _intraday["scan_status"] = "scanning"

        try:
            top = screen_top(n=20)
            with _intraday_lock:
                _intraday["scan_results"] = top
                _intraday["last_scan"]    = datetime.now().strftime("%H:%M:%S")
                _intraday["scan_status"]  = "done"

            if enabled and not squared_off:
                _execute_intraday_trades(top)

        except Exception as exc:
            log.error("[Intraday scan] %s", exc)
            with _intraday_lock:
                _intraday["scan_status"] = "error"

        time.sleep(config.SCAN_INTERVAL_SECS)


def _execute_intraday_trades(top_stocks: list) -> None:
    """Auto-buy top BUY signals and auto-sell stale positions."""
    with _intraday_lock:
        daily_pnl = _intraday["daily_pnl"]

    if daily_pnl < -(config.INITIAL_CAPITAL * config.MAX_INTRADAY_LOSS):
        log.warning("[AutoTrade] Daily loss limit hit — pausing trades")
        return

    positions  = pf.get_positions()
    prices_now = {}

    for item in top_stocks[:10]:
        sym  = item["symbol"]
        snap = intraday_snapshot(sym)
        if not snap:
            continue
        prices_now[sym] = snap["price"]
        signal = intraday_signal(snap)

        if signal == "BUY" and sym not in positions:
            if len(positions) >= config.MAX_INTRADAY_POS:
                continue
            price = snap["price"]
            cash  = pf.get_cash()
            qty   = max(1, int(cash * config.INTRADAY_QTY_PCT / price))
            r = executor.buy(sym, price, f"AutoIntraday: score={item['score']}")
            if r.get("ok"):
                with _intraday_lock:
                    _intraday["trades_today"] += 1
                log.info("[AutoTrade] BUY %s %d @ %.2f", sym, qty, price)

        elif signal == "SELL" and sym in positions:
            price = snap["price"]
            r = executor.sell(sym, price, "AutoIntraday: SELL signal")
            if r.get("ok"):
                pnl = r.get("pnl", 0) or 0
                with _intraday_lock:
                    _intraday["daily_pnl"]   += pnl
                    _intraday["trades_today"] += 1
                log.info("[AutoTrade] SELL %s @ %.2f  P&L=%.2f", sym, price, pnl)

    executor.check_stop_loss_take_profit(prices_now)


def _background_squareoff_watch() -> None:
    """At 3:15 PM IST, close all open intraday positions."""
    while True:
        now = _ist_now()
        sq  = config.SQUARE_OFF_TIME
        if now.weekday() < 5 and (now.hour, now.minute) >= sq:
            with _intraday_lock:
                if not _intraday["squared_off"]:
                    _intraday["squared_off"] = True
                    _intraday["enabled"]     = False   # stop new trades

            _do_squareoff()

        # Reset squared_off flag at midnight for next day
        if now.hour == 0 and now.minute < 2:
            with _intraday_lock:
                _intraday["squared_off"] = False
                _intraday["daily_pnl"]   = 0.0
                _intraday["trades_today"]= 0

        time.sleep(30)


def _do_squareoff() -> None:
    """Close all open positions at market price."""
    positions = pf.get_positions()
    if not positions:
        return
    log.info("[SquareOff] Closing %d positions at 3:15 PM IST", len(positions))
    prices = stock_data.get_batch_prices(list(positions.keys()))
    for sym in list(positions.keys()):
        price = prices.get(sym)
        if price is None:
            price = stock_data.get_current_price(sym)
        if price:
            r = executor.sell(sym, price, "EOD square-off 3:15 PM")
            pnl = r.get("pnl", 0) or 0
            with _intraday_lock:
                _intraday["daily_pnl"] += pnl
            log.info("[SquareOff] %s @ %.2f  P&L=%.2f", sym, price, pnl)


def _background_keepalive() -> None:
    """
    Ping our own health endpoint every 10 min so Render's free tier doesn't
    spin down the server during market hours.  No-op when not on Render.
    """
    url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not url:
        return
    log.info("[Keepalive] Active — pinging %s every 10 min", url)
    while True:
        time.sleep(600)
        try:
            urllib.request.urlopen(f"{url}/api/market_status", timeout=15)
            log.debug("[Keepalive] OK")
        except Exception as exc:
            log.debug("[Keepalive] %s", exc)


def _restore_auto_state() -> None:
    """Re-apply the auto-trade preference saved in the DB after a restart."""
    saved = db.get_setting("auto_trade_enabled", "0")
    if saved == "1":
        with _intraday_lock:
            _intraday["enabled"] = True
        log.info("[AutoTrade] Restored: ENABLED (from DB)")


def _start_background_threads() -> None:
    for fn in (_background_news_refresh, _background_price_refresh,
               _background_intraday_scan, _background_squareoff_watch,
               _background_keepalive):
        threading.Thread(target=fn, daemon=True).start()


def _maybe_train_ml() -> None:
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
        watchlist   = config.WATCHLIST,
        mode        = config.TRADING_MODE,
        market      = config.MARKET,
        initial_cap = config.INITIAL_CAPITAL,
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


# ── Intraday / screener APIs ───────────────────────────────────────────────

@app.route("/api/screener")
def api_screener():
    """Return top 20 NSE stocks by intraday momentum (cached from last scan)."""
    with _intraday_lock:
        results    = list(_intraday["scan_results"])
        last_scan  = _intraday["last_scan"]
        scan_status= _intraday["scan_status"]
    return jsonify({
        "results":    results,
        "last_scan":  last_scan,
        "status":     scan_status,
        "trading_hours": _is_trading_hours(),
    })


@app.route("/api/intraday/<symbol>")
def api_intraday_symbol(symbol):
    """Return live intraday snapshot for a single NSE symbol."""
    snap = intraday_snapshot(symbol.upper())
    if not snap:
        return jsonify({"error": "No intraday data available"}), 404
    snap["signal"] = intraday_signal(snap)
    return jsonify(snap)


@app.route("/api/intraday_state")
def api_intraday_state():
    with _intraday_lock:
        state = dict(_intraday)
    state["trading_hours"] = _is_trading_hours()   # bot may place trades
    state["market_open"]   = _nse_market_open()    # actual NSE session
    state["ist_time"]      = _ist_now().strftime("%H:%M:%S")
    return jsonify(state)


@app.route("/api/auto_trade", methods=["POST"])
def api_auto_trade():
    """Toggle auto-trading on or off. Persists across server restarts."""
    data    = request.get_json() or {}
    enabled = bool(data.get("enabled", False))
    with _intraday_lock:
        _intraday["enabled"] = enabled
    db.set_setting("auto_trade_enabled", "1" if enabled else "0")
    log.info("[AutoTrade] %s", "ENABLED" if enabled else "DISABLED")
    return jsonify({"ok": True, "enabled": enabled})


@app.route("/api/search")
def api_search():
    """Fuzzy search NSE stocks by symbol or company name."""
    q = request.args.get("q", "").strip()
    if len(q) < 1:
        return jsonify([])
    results = nse_search(q, limit=15)
    # Enrich with live price if cached
    with _prices_lock:
        cached = dict(_prices_cache)
    for r in results:
        r["price"] = cached.get(r["symbol"])
    return jsonify(results)


@app.route("/api/squareoff", methods=["POST"])
def api_squareoff():
    """Manually trigger EOD square-off (close all open positions)."""
    _do_squareoff()
    with _intraday_lock:
        pnl = _intraday["daily_pnl"]
    return jsonify({"ok": True, "daily_pnl": round(pnl, 2)})


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
    _restore_auto_state()
    _start_background_threads()
    threading.Thread(target=_maybe_train_ml, daemon=True).start()
    return app


if __name__ == "__main__":
    db.init_db()
    _restore_auto_state()
    _start_background_threads()
    threading.Thread(target=_maybe_train_ml, daemon=True).start()
    log.info("Starting StockBot web server at http://%s:%d", config.WEB_HOST, config.WEB_PORT)
    app.run(
        host  = config.WEB_HOST,
        port  = config.WEB_PORT,
        debug = config.WEB_DEBUG,
        use_reloader=False,    # prevent double-init of background threads
    )
