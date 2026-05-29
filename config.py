import os
from dotenv import load_dotenv

load_dotenv()

# --- Primary market (kept for backward-compat; per-symbol market via SYMBOL_MARKET) ---
# 'NSE' = National Stock Exchange of India
# 'US'  = US markets
MARKET = os.getenv("MARKET", "NSE")

# --- Multi-market watchlist ---
# Each entry: (ticker, exchange).  Code uses SYMBOL_MARKET for per-symbol logic.
WATCHLIST_CONFIG = [
    # ── Indian stocks (NSE) ──────────────────────────
    ("RELIANCE",   "NSE"),   # Reliance Industries
    ("TCS",        "NSE"),   # Tata Consultancy Services
    ("INFY",       "NSE"),   # Infosys
    ("HDFCBANK",   "NSE"),   # HDFC Bank
    ("ICICIBANK",  "NSE"),   # ICICI Bank
    ("SBIN",       "NSE"),   # State Bank of India
    ("WIPRO",      "NSE"),   # Wipro
    ("HINDUNILVR", "NSE"),   # Hindustan Unilever
    ("ITC",        "NSE"),   # ITC
    ("BAJFINANCE", "NSE"),   # Bajaj Finance
    ("HCLTECH",    "NSE"),   # HCL Technologies
    ("ADANIENT",   "NSE"),   # Adani Enterprises
    # ── US stocks (NASDAQ / NYSE) ────────────────────
    ("AAPL",       "NASDAQ"),  # Apple
    ("MSFT",       "NASDAQ"),  # Microsoft
    ("GOOGL",      "NASDAQ"),  # Alphabet / Google
    ("NVDA",       "NASDAQ"),  # NVIDIA
    ("TSLA",       "NASDAQ"),  # Tesla
    ("META",       "NASDAQ"),  # Meta Platforms
]

# Convenience flat list and per-symbol exchange map
WATCHLIST    = [s for s, _ in WATCHLIST_CONFIG]
SYMBOL_MARKET = {s: m for s, m in WATCHLIST_CONFIG}

# --- Market trading hours (used for open/closed status) ---
# Each entry: timezone, (open_hour, open_min), (close_hour, close_min)
MARKET_HOURS = {
    "NSE":    {"tz": "Asia/Kolkata",     "open": (9,  15), "close": (15, 30)},
    "NYSE":   {"tz": "America/New_York", "open": (9,  30), "close": (16,  0)},
    "NASDAQ": {"tz": "America/New_York", "open": (9,  30), "close": (16,  0)},
    "LSE":    {"tz": "Europe/London",    "open": (8,   0), "close": (16, 30)},
    "TSX":    {"tz": "America/Toronto",  "open": (9,  30), "close": (16,  0)},
}

# --- Trading mode ---
# 'simulation' : virtual ₹ portfolio tracked in SQLite — no real money
# 'zerodha'    : live/paper via Zerodha Kite Connect API
TRADING_MODE = os.getenv("TRADING_MODE", "simulation")

# --- Zerodha Kite Connect ---
# Get your API key + secret from https://developers.kite.trade/
ZERODHA_API_KEY    = os.getenv("ZERODHA_API_KEY", "")
ZERODHA_API_SECRET = os.getenv("ZERODHA_API_SECRET", "")
# Product type: CNC (delivery/long-term) | MIS (intraday margin)
ZERODHA_PRODUCT    = os.getenv("ZERODHA_PRODUCT", "CNC")

# --- Simulation ---
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "20000"))

# --- Intraday settings ---
INTRADAY_MODE       = True          # run intraday auto-trading during market hours
SCAN_INTERVAL_SECS  = 300           # scan + trade every 5 minutes
SQUARE_OFF_TIME     = (15, 15)      # (hour, minute) IST — close all positions before 3:30
MAX_INTRADAY_LOSS   = 0.03          # stop auto-trading if daily P&L drops below -3%
MAX_INTRADAY_POS    = 5             # max concurrent intraday positions
INTRADAY_QTY_PCT    = 0.18          # allocate ~18% of free cash per trade

# --- Risk management ---
MAX_POSITION_PCT = 0.18    # max 18% of portfolio per position (intraday sizing)
STOP_LOSS_PCT    = 0.015   # 1.5% intraday stop-loss
TAKE_PROFIT_PCT  = 0.03    # 3% intraday take-profit
MAX_POSITIONS    = 5       # maximum concurrent open positions

# --- Technical indicator parameters ---
RSI_PERIOD     = 14
RSI_OVERSOLD   = 30
RSI_OVERBOUGHT = 70

SMA_SHORT = 20
SMA_LONG  = 50

EMA_FAST = 12
EMA_SLOW = 26
MACD_SIGNAL_PERIOD = 9

BB_PERIOD = 20
BB_STD    = 2.0

VOLUME_AVG_PERIOD = 20

# --- Signal thresholds (base values — adjusted dynamically by regime detector) ---
# Calibrated from first live run (2026-05-24):
BUY_THRESHOLD  =  0.32
SELL_THRESHOLD = -0.27

# SIGNAL_WEIGHTS kept for reference (signals.py uses internal _WEIGHTS_WITH_ML / _WEIGHTS_NO_ML)
SIGNAL_WEIGHTS = {
    "rsi":           0.15,
    "macd":          0.25,
    "sma_crossover": 0.10,
    "bollinger":     0.08,
    "volume":        0.04,
    "momentum":      0.08,
    "sentiment":     0.12,
    "ml":            0.18,   # XGBoost P(BUY)-P(SELL); 0.00 when model not trained
}

# --- ML model ---
ML_FORWARD_DAYS    = 5       # label horizon: predict 5-day forward return
ML_LABEL_THRESHOLD = 0.015   # ±1.5% to classify as BUY/SELL
ML_RETRAIN_DAYS    = 7       # retrain weekly

# --- Historical data ---
DATA_PERIOD   = "6mo"
DATA_INTERVAL = "1d"

# --- News ---
NEWS_LOOKBACK_HOURS = 24
MAX_NEWS_PER_SYMBOL = 20

# --- Database ---
DB_PATH = os.path.join(os.path.dirname(__file__), "stockbot.db")

# --- Logging ---
LOG_FILE  = os.path.join(os.path.dirname(__file__), "stockbot.log")
LOG_LEVEL = "INFO"

# --- Web app ---
# On Render (RENDER env var is set automatically), bind to all interfaces.
# Locally defaults to 127.0.0.1 (safe).
WEB_HOST  = os.getenv("WEB_HOST", "0.0.0.0")
# PORT is set by Render automatically; WEB_PORT is the local fallback
WEB_PORT  = int(os.getenv("PORT", os.getenv("WEB_PORT", "5000")))
WEB_DEBUG = os.getenv("WEB_DEBUG", "false").lower() == "true"
WEB_SECRET = os.getenv("WEB_SECRET", "change-me-in-production")

# --- Schedule (IST, 24-h) ---
SCHEDULE_ANALYSIS_TIME = "09:00"
SCHEDULE_TRADE_TIME    = "09:20"
SCHEDULE_MIDDAY_TIME   = "12:00"
SCHEDULE_EOD_TIME      = "15:35"
