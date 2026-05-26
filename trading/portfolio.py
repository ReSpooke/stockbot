"""
Portfolio state read from the SQLite database.

Provides a thin, stateless view layer so other modules can read
portfolio info without reaching into the database directly.
"""

from database import db
from utils.logger import log


def summary(current_prices: dict[str, float]) -> dict:
    """
    Return a snapshot of the portfolio:
      cash, positions, market_value, total_value, total_pnl, pnl_pct
    """
    cash      = db.get_cash()
    positions = db.get_positions()

    pos_list = []
    market_value = 0.0

    for sym, pos in positions.items():
        price = current_prices.get(sym)
        qty   = float(pos["quantity"])
        cost  = float(pos["avg_cost"])
        if price:
            mv      = price * qty
            pnl     = mv - cost * qty
            pnl_pct = (pnl / (cost * qty)) * 100 if cost > 0 else 0
        else:
            mv = cost * qty
            pnl = 0.0
            pnl_pct = 0.0

        market_value += mv
        pos_list.append({
            "symbol":       sym,
            "quantity":     qty,
            "avg_cost":     cost,
            "current_price":price,
            "market_value": round(mv, 2),
            "unrealised_pnl": round(pnl, 2),
            "pnl_pct":      round(pnl_pct, 2),
        })

    total_value = cash + market_value
    initial     = db.get_cash()  # read-only; for pnl% we need initial capital
    # compute total pnl based on initial capital from config
    import config
    total_pnl     = total_value - config.INITIAL_CAPITAL
    total_pnl_pct = (total_pnl / config.INITIAL_CAPITAL) * 100

    return {
        "cash":           round(cash, 2),
        "market_value":   round(market_value, 2),
        "total_value":    round(total_value, 2),
        "total_pnl":      round(total_pnl, 2),
        "total_pnl_pct":  round(total_pnl_pct, 2),
        "positions":      pos_list,
        "n_positions":    len(pos_list),
    }


def get_positions() -> dict:
    return db.get_positions()


def get_cash() -> float:
    return db.get_cash()


def can_open_position(symbol: str, price: float) -> tuple[bool, str]:
    """
    Check whether risk rules allow opening a new position in *symbol*.
    Returns (True, "") or (False, reason).
    """
    import config

    positions = db.get_positions()

    if symbol in positions:
        return False, f"Already holding {symbol}"

    if len(positions) >= config.MAX_POSITIONS:
        return False, f"Max positions ({config.MAX_POSITIONS}) reached"

    cash = db.get_cash()
    total_value = cash  # rough lower bound
    for sym, pos in positions.items():
        total_value += float(pos["avg_cost"]) * float(pos["quantity"])

    max_spend = total_value * config.MAX_POSITION_PCT
    if max_spend < price:
        return False, f"Position size too small (max ${max_spend:.2f} < ${price:.2f})"

    if cash < price:
        return False, "Insufficient cash"

    return True, ""


def position_size(price: float) -> float:
    """Shares to buy given risk rules (whole shares only)."""
    import config

    cash = db.get_cash()
    positions = db.get_positions()
    total_value = cash
    for sym, pos in positions.items():
        total_value += float(pos["avg_cost"]) * float(pos["quantity"])

    max_spend = min(total_value * config.MAX_POSITION_PCT, cash * 0.95)
    if max_spend <= 0 or price <= 0:
        return 0.0
    return max(1.0, int(max_spend / price))
