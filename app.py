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
from data.nse_stocks import screen_all, screen_top, search as nse_search
from analysis import signals as sig_gen
from analysis import sentiment as sent
from analysis.intraday import intraday_snapshot, intraday_signal
from analysis.decision import evaluate as decide
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

# ── Intraday bot state ────────────────────────────────────────────────────

_intraday = {
    "last_scan":     None,   # HH:MM:SS of last completed scan
    "scan_results":  [],     # all scanned stocks sorted by momentum score
    "daily_pnl":     0.0,   # realised P&L today (₹)
    "trades_today":  0,
    "squared_off":   False,
    "scan_status":   "idle",
    "scan_started":  None,
}
_intraday_lock = threading.Lock()

# ── Bot decision log (newest first, max 300 entries) ─────────────────────

_decision_log: list = []
_decision_lock = threading.Lock()


def _log(entries: list) -> None:
    """Prepend entries to the decision log; trim to 300."""
    with _decision_lock:
        _decision_log[:0] = entries          # prepend
        del _decision_log[300:]

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
    """Every SCAN_INTERVAL_SECS: scan ALL NSE stocks → run decision engine → trade."""
    while True:
        if not _is_trading_hours():
            time.sleep(30)
            continue

        t0 = time.monotonic()
        now = _ist_now()
        with _intraday_lock:
            squared_off = _intraday["squared_off"]
            _intraday["scan_status"]  = "scanning"
            _intraday["scan_started"] = now.strftime("%H:%M:%S")

        try:
            all_results = screen_all()           # scan every stock in UNIVERSE
            elapsed = time.monotonic() - t0
            with _intraday_lock:
                _intraday["scan_results"] = all_results
                _intraday["last_scan"]    = datetime.now().strftime("%H:%M:%S")
                _intraday["scan_status"]  = "done"
            log.info("[Bot] Scan done in %.1fs — %d stocks", elapsed, len(all_results))

            if not squared_off:
                _run_decision_cycle(all_results, now.hour, now.minute)

        except Exception as exc:
            log.error("[Bot] Scan error: %s", exc)
            with _intraday_lock:
                _intraday["scan_status"] = "error"

        time.sleep(config.SCAN_INTERVAL_SECS)


def _run_decision_cycle(all_results: list, ist_hour: int, ist_minute: int) -> None:
    """
    Run the decision engine on every scanned stock.
    Execute trades for BUY/SELL decisions; log every evaluation.
    """
    with _intraday_lock:
        daily_pnl = _intraday["daily_pnl"]

    if daily_pnl < -(config.INITIAL_CAPITAL * config.MAX_INTRADAY_LOSS):
        log.warning("[Bot] Daily loss limit reached — skipping cycle")
        _log([{"time": _ist_now().strftime("%H:%M:%S"), "symbol": "—",
                "action": "PAUSED", "score": 0, "confidence": "—", "price": 0,
                "pct_chg": 0, "reasons": [f"Daily loss limit ₹{daily_pnl:.0f}"],
                "traded": False, "pnl": None}])
        return

    positions  = pf.get_positions()
    cash       = pf.get_cash()
    prices_now = {r["symbol"]: r["price"] for r in all_results if r.get("price")}
    cycle_log  = []
    buy_queue  = []   # (decision, item) — executed after SELLs, sorted by score

    for item in all_results:
        sym   = item.get("symbol")
        price = item.get("price")
        if not sym or not price:
            continue

        dec = decide(item, ist_hour, ist_minute)
        entry = {
            "time":       _ist_now().strftime("%H:%M:%S"),
            "symbol":     sym,
            "action":     dec["action"],
            "score":      dec["score"],
            "confidence": dec["confidence"],
            "price":      price,
            "pct_chg":    item.get("pct_chg", 0),
            "reasons":    dec["reasons"],
            "traded":     False,
            "pnl":        None,
        }

        if dec["action"] == "SELL" and sym in positions:
            r = executor.sell(sym, price,
                              f"Bot SELL score={dec['score']} | " +
                              "; ".join(dec["reasons"][:2]))
            if r.get("ok"):
                pnl = r.get("pnl", 0) or 0
                entry["traded"] = True
                entry["pnl"]    = round(pnl, 2)
                with _intraday_lock:
                    _intraday["daily_pnl"]   += pnl
                    _intraday["trades_today"] += 1
                log.info("[Bot] SELL %s @ %.2f  score=%d  P&L=%.2f",
                         sym, price, dec["score"], pnl)

        elif dec["action"] == "BUY" and sym not in positions:
            buy_queue.append((dec, item, entry))

        cycle_log.append(entry)

    # Execute BUY candidates sorted by highest conviction first
    buy_queue.sort(key=lambda x: x[0]["score"], reverse=True)

    for dec, item, entry in buy_queue:
        sym   = item["symbol"]
        price = item["price"]

        # Re-read position count after any SELLs above
        if len(pf.get_positions()) >= config.MAX_INTRADAY_POS:
            entry["reasons"].append("Skipped — max positions")
            continue

        qty  = max(1, int(cash * dec["qty_pct"] / price))
        cost = qty * price
        if cost > cash * 0.95:
            entry["reasons"].append("Skipped — insufficient cash")
            continue

        r = executor.buy(sym, price,
                         f"Bot BUY score={dec['score']} {dec['confidence']} | " +
                         "; ".join(dec["reasons"][:2]))
        if r.get("ok"):
            cash -= cost
            entry["traded"] = True
            with _intraday_lock:
                _intraday["trades_today"] += 1
            log.info("[Bot] BUY %s %d @ %.2f  score=%d  %s",
                     sym, qty, price, dec["score"], dec["confidence"])

    # SL/TP sweep — no extra API calls needed
    executor.check_stop_loss_take_profit(prices_now)

    _log(cycle_log)


def _background_squareoff_watch() -> None:
    """At 3:15 PM IST, close all open intraday positions."""
    while True:
        now = _ist_now()
        sq  = config.SQUARE_OFF_TIME
        if now.weekday() < 5 and (now.hour, now.minute) >= sq:
            with _intraday_lock:
                already_done = _intraday["squared_off"]
            if not already_done:
                with _intraday_lock:
                    _intraday["squared_off"] = True
                _do_squareoff()

        if now.hour == 0 and now.minute < 2:
            with _intraday_lock:
                _intraday["squared_off"]  = False
                _intraday["daily_pnl"]    = 0.0
                _intraday["trades_today"] = 0

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
    """Return top 20 Nifty 50 stocks by intraday momentum (cached from last scan)."""
    with _intraday_lock:
        results      = list(_intraday["scan_results"])
        last_scan    = _intraday["last_scan"]
        scan_status  = _intraday["scan_status"]
        scan_started = _intraday["scan_started"]
    return jsonify({
        "results":       results,
        "last_scan":     last_scan,
        "scan_status":   scan_status,
        "scan_started":  scan_started,
        "market_open":   _nse_market_open(),
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
        # Expose only top 30 for display (full list can be large)
        state["scan_results"] = state["scan_results"][:30]
    state["trading_hours"] = _is_trading_hours()
    state["market_open"]   = _nse_market_open()
    state["ist_time"]      = _ist_now().strftime("%H:%M:%S")
    state["always_auto"]   = True
    return jsonify(state)


@app.route("/api/bot_log")
def api_bot_log():
    """Return the bot's decision log (newest first)."""
    limit = int(request.args.get("limit", 100))
    with _decision_lock:
        entries = list(_decision_log[:limit])
    return jsonify(entries)


@app.route("/api/auto_trade", methods=["POST"])
def api_auto_trade():
    """Kept for backwards-compat. Bot always runs during market hours."""
    return jsonify({"ok": True, "enabled": True, "note": "Bot always auto-trades"})


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
