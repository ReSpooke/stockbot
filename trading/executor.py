"""
Trade execution — supports 'simulation' and 'zerodha' modes.
All public functions return a uniform result dict.
"""

import config
from database import db
from trading import portfolio as pf
from utils.logger import log


# ── Public ─────────────────────────────────────────────────────────────────

def buy(symbol: str, price: float, reason: str = "") -> dict:
    allowed, msg = pf.can_open_position(symbol, price)
    if not allowed:
        log.info("[%s] BUY skipped — %s", symbol, msg)
        return _r(False, symbol, "buy", 0, price, msg)

    qty = pf.position_size(price)
    if qty <= 0:
        return _r(False, symbol, "buy", 0, price, "Position size is 0")

    if config.TRADING_MODE == "simulation":
        return _sim_buy(symbol, qty, price, reason)
    elif config.TRADING_MODE == "zerodha":
        return _zerodha_buy(symbol, int(qty), price, reason)
    return _r(False, symbol, "buy", qty, price, f"Unknown mode: {config.TRADING_MODE}")


def sell(symbol: str, price: float, reason: str = "") -> dict:
    positions = db.get_positions()
    if symbol not in positions:
        return _r(False, symbol, "sell", 0, price, "No open position")

    qty = float(positions[symbol]["quantity"])

    if config.TRADING_MODE == "simulation":
        return _sim_sell(symbol, qty, price, reason)
    elif config.TRADING_MODE == "zerodha":
        return _zerodha_sell(symbol, int(qty), price, reason)
    return _r(False, symbol, "sell", qty, price, f"Unknown mode: {config.TRADING_MODE}")


def check_stop_loss_take_profit(current_prices: dict) -> list:
    results = []
    for sym, pos in db.get_positions().items():
        price    = current_prices.get(sym)
        avg_cost = float(pos["avg_cost"])
        if not price or avg_cost <= 0:
            continue
        change = (price - avg_cost) / avg_cost
        if change <= -config.STOP_LOSS_PCT:
            log.warning("[%s] Stop-loss %.1f%%", sym, change * 100)
            results.append(sell(sym, price, f"Stop-loss ({change*100:.1f}%)"))
        elif change >= config.TAKE_PROFIT_PCT:
            log.info("[%s] Take-profit %.1f%%", sym, change * 100)
            results.append(sell(sym, price, f"Take-profit ({change*100:.1f}%)"))
    return results


# ── Simulation backend ──────────────────────────────────────────────────────

def _sim_buy(symbol: str, qty: float, price: float, reason: str) -> dict:
    cost = qty * price
    cash = db.get_cash()
    if cost > cash:
        qty  = int(cash / price)
        cost = qty * price
    if qty <= 0:
        return _r(False, symbol, "buy", 0, price, "Insufficient cash")
    db.set_cash(cash - cost)
    db.upsert_position(symbol, qty, price)
    db.save_trade(symbol, "buy", qty, price, reason=reason)
    msg = f"[SIM] Bought {qty} × {symbol} @ ₹{price:.2f}"
    log.info(msg)
    return _r(True, symbol, "buy", qty, price, msg)


def _sim_sell(symbol: str, qty: float, price: float, reason: str) -> dict:
    avg_cost = float(db.get_positions()[symbol]["avg_cost"])
    proceeds = qty * price
    pnl      = (price - avg_cost) * qty
    db.set_cash(db.get_cash() + proceeds)
    db.delete_position(symbol)
    db.save_trade(symbol, "sell", qty, price, pnl=pnl, reason=reason)
    msg = f"[SIM] Sold {qty} × {symbol} @ ₹{price:.2f}  P&L: ₹{pnl:+.2f}"
    log.info(msg)
    return _r(True, symbol, "sell", qty, price, msg)


# ── Zerodha backend ─────────────────────────────────────────────────────────

def _zerodha_buy(symbol: str, qty: int, price: float, reason: str) -> dict:
    from trading import zerodha
    try:
        if not zerodha.is_authenticated():
            return _r(False, symbol, "buy", qty, price,
                      "Not authenticated with Zerodha — open /zerodha/login")
        order_id = zerodha.place_market_order(symbol, "buy", qty)
        db.upsert_position(symbol, qty, price)
        db.save_trade(symbol, "buy", qty, price, reason=reason)
        msg = f"Zerodha BUY {qty} × {symbol} | order_id={order_id}"
        log.info(msg)
        return _r(True, symbol, "buy", qty, price, msg)
    except Exception as exc:
        log.error("[%s] Zerodha buy failed: %s", symbol, exc)
        return _r(False, symbol, "buy", qty, price, str(exc))


def _zerodha_sell(symbol: str, qty: int, price: float, reason: str) -> dict:
    from trading import zerodha
    try:
        if not zerodha.is_authenticated():
            return _r(False, symbol, "sell", qty, price,
                      "Not authenticated with Zerodha — open /zerodha/login")
        positions = db.get_positions()
        avg_cost  = float(positions[symbol]["avg_cost"])
        pnl       = (price - avg_cost) * qty
        order_id  = zerodha.place_market_order(symbol, "sell", qty)
        db.delete_position(symbol)
        db.save_trade(symbol, "sell", qty, price, pnl=pnl, reason=reason)
        msg = f"Zerodha SELL {qty} × {symbol} | P&L: ₹{pnl:+.2f} | order_id={order_id}"
        log.info(msg)
        return _r(True, symbol, "sell", qty, price, msg)
    except Exception as exc:
        log.error("[%s] Zerodha sell failed: %s", symbol, exc)
        return _r(False, symbol, "sell", qty, price, str(exc))


# ── Helper ──────────────────────────────────────────────────────────────────

def _r(ok, symbol, side, qty, price, msg) -> dict:
    return {"ok": ok, "symbol": symbol, "side": side,
            "quantity": qty, "price": price, "total": qty * price, "message": msg}
