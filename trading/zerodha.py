"""
Zerodha Kite Connect integration.

Setup (one-time):
  1. Create a developer app at https://developers.kite.trade/
  2. Add ZERODHA_API_KEY and ZERODHA_API_SECRET to your .env file
  3. Open http://localhost:5000/zerodha/login in your browser each morning
     to authenticate (access tokens expire at midnight IST)
"""

import json
from datetime import date
from pathlib import Path
from typing import Optional

import config
from utils.logger import log

_TOKEN_FILE = Path(__file__).parent.parent / ".zerodha_token.json"


# ── Authentication ─────────────────────────────────────────────────────────

def get_login_url() -> str:
    kite = _kite_client()
    return kite.login_url()


def complete_login(request_token: str) -> Optional[str]:
    """Exchange request_token → access_token, persist for today."""
    kite = _kite_client()
    try:
        session_data = kite.generate_session(
            request_token, api_secret=config.ZERODHA_API_SECRET
        )
        token = session_data["access_token"]
        _save_token(token, session_data.get("user_name", ""), session_data.get("email", ""))
        kite.set_access_token(token)
        log.info("Zerodha authenticated: %s", session_data.get("user_name", ""))
        return token
    except Exception as exc:
        log.error("Zerodha login failed: %s", exc)
        return None


def is_authenticated() -> bool:
    return _load_token() is not None


def get_user_info() -> dict:
    data = _load_full_token_data()
    if not data:
        return {}
    return {"name": data.get("user_name", ""), "email": data.get("email", "")}


# ── Trading ────────────────────────────────────────────────────────────────

def get_ltp(symbols: list) -> dict:
    """Last traded price for a list of NSE symbols. Returns {symbol: price}."""
    try:
        kite        = _authenticated_kite()
        instruments = [f"NSE:{s}" for s in symbols]
        quotes      = kite.ltp(instruments)
        return {
            s: quotes[f"NSE:{s}"]["last_price"]
            for s in symbols
            if f"NSE:{s}" in quotes
        }
    except Exception as exc:
        log.warning("Zerodha LTP failed: %s", exc)
        return {}


def place_market_order(symbol: str, side: str, quantity: int) -> str:
    """
    Place a market order. Returns order_id string.
    side: 'buy' | 'sell'
    """
    kite = _authenticated_kite()
    from kiteconnect import KiteConnect

    tx_type = (
        kite.TRANSACTION_TYPE_BUY
        if side.lower() == "buy"
        else kite.TRANSACTION_TYPE_SELL
    )
    product = (
        kite.PRODUCT_CNC
        if config.ZERODHA_PRODUCT == "CNC"
        else kite.PRODUCT_MIS
    )

    order_id = kite.place_order(
        tradingsymbol   = symbol,
        exchange        = kite.EXCHANGE_NSE,
        transaction_type= tx_type,
        quantity        = int(quantity),
        product         = product,
        order_type      = kite.ORDER_TYPE_MARKET,
        variety         = kite.VARIETY_REGULAR,
    )
    log.info("Zerodha order placed: %s %s x%d → order_id=%s", side.upper(), symbol, quantity, order_id)
    return str(order_id)


def get_positions() -> list:
    """Return current Zerodha positions (net)."""
    try:
        kite = _authenticated_kite()
        return kite.positions().get("net", [])
    except Exception as exc:
        log.warning("Could not fetch Zerodha positions: %s", exc)
        return []


def get_holdings() -> list:
    """Return Zerodha holdings (long-term CNC positions)."""
    try:
        kite = _authenticated_kite()
        return kite.holdings()
    except Exception as exc:
        log.warning("Could not fetch Zerodha holdings: %s", exc)
        return []


# ── Internal helpers ───────────────────────────────────────────────────────

def _kite_client():
    try:
        from kiteconnect import KiteConnect
    except ImportError:
        raise RuntimeError("kiteconnect not installed. Run: pip install kiteconnect")
    if not config.ZERODHA_API_KEY:
        raise RuntimeError(
            "ZERODHA_API_KEY not set in .env — "
            "create an app at https://developers.kite.trade/"
        )
    return KiteConnect(api_key=config.ZERODHA_API_KEY)


def _authenticated_kite():
    token = _load_token()
    if not token:
        raise RuntimeError("Not authenticated with Zerodha. Open /zerodha/login in the browser.")
    kite = _kite_client()
    kite.set_access_token(token)
    return kite


def _save_token(token: str, user_name: str = "", email: str = "") -> None:
    _TOKEN_FILE.write_text(
        json.dumps({
            "access_token": token,
            "user_name":    user_name,
            "email":        email,
            "date":         str(date.today()),
        }),
        encoding="utf-8",
    )


def _load_token() -> Optional[str]:
    data = _load_full_token_data()
    return data.get("access_token") if data else None


def _load_full_token_data() -> Optional[dict]:
    if not _TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
        if data.get("date") == str(date.today()):
            return data
    except Exception:
        pass
    return None
