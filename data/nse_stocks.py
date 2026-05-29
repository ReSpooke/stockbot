"""
NSE stock universe — Nifty 500 symbols + intraday screener.

screen_top(n)   → returns top-N stocks by intraday momentum score
search(query)   → fuzzy search by symbol or company name
"""

import numpy as np
import pandas as pd
import pytz
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils.logger import log
from analysis.indicators import compute_snapshot

_IST = pytz.timezone("Asia/Kolkata")

# ── Nifty 500 universe ────────────────────────────────────────────────────────
# Format: (NSE_SYMBOL, Company Name)
_UNIVERSE = [
    # Nifty 50
    ("RELIANCE","Reliance Industries"),("TCS","Tata Consultancy"),("HDFCBANK","HDFC Bank"),
    ("INFY","Infosys"),("ICICIBANK","ICICI Bank"),("HINDUNILVR","Hindustan Unilever"),
    ("ITC","ITC Limited"),("SBIN","State Bank of India"),("BHARTIARTL","Bharti Airtel"),
    ("KOTAKBANK","Kotak Mahindra Bank"),("LT","Larsen & Toubro"),("AXISBANK","Axis Bank"),
    ("HCLTECH","HCL Technologies"),("ASIANPAINT","Asian Paints"),("MARUTI","Maruti Suzuki"),
    ("BAJFINANCE","Bajaj Finance"),("SUNPHARMA","Sun Pharma"),("TATAMOTORS","Tata Motors"),
    ("WIPRO","Wipro"),("ULTRACEMCO","UltraTech Cement"),("INDUSINDBK","IndusInd Bank"),
    ("TECHM","Tech Mahindra"),("ONGC","ONGC"),("ADANIENT","Adani Enterprises"),
    ("TITAN","Titan Company"),("POWERGRID","Power Grid"),("NTPC","NTPC"),
    ("BAJAJFINSV","Bajaj Finserv"),("NESTLEIND","Nestle India"),("JSWSTEEL","JSW Steel"),
    ("TATASTEEL","Tata Steel"),("HDFCLIFE","HDFC Life"),("M&M","Mahindra & Mahindra"),
    ("GRASIM","Grasim Industries"),("DIVISLAB","Divi's Laboratories"),("CIPLA","Cipla"),
    ("DRREDDY","Dr Reddy's"),("EICHERMOT","Eicher Motors"),("APOLLOHOSP","Apollo Hospitals"),
    ("BPCL","BPCL"),("COALINDIA","Coal India"),("ADANIPORTS","Adani Ports"),
    ("HINDALCO","Hindalco"),("TATACONSUM","Tata Consumer"),("BRITANNIA","Britannia"),
    ("SBILIFE","SBI Life"),("HEROMOTOCO","Hero MotoCorp"),("SHREECEM","Shree Cement"),
    ("UPL","UPL"),("BAJAJFINSV","Bajaj Finserv"),
    # Nifty Next 50
    ("PIDILITIND","Pidilite Industries"),("HAVELLS","Havells India"),("DMART","DMart"),
    ("SIEMENS","Siemens"),("AMBUJACEM","Ambuja Cements"),("ACC","ACC"),
    ("BERGEPAINT","Berger Paints"),("COLPAL","Colgate-Palmolive"),("DABUR","Dabur India"),
    ("GODREJCP","Godrej Consumer"),("MARICO","Marico"),("MUTHOOTFIN","Muthoot Finance"),
    ("PGHH","P&G Hygiene"),("TORNTPHARM","Torrent Pharma"),("BIOCON","Biocon"),
    ("AUROPHARMA","Aurobindo Pharma"),("LUPIN","Lupin"),("ALKEM","Alkem Labs"),
    ("IPCALAB","IPCA Laboratories"),("JUBLFOOD","Jubilant Foodworks"),
    ("TRENT","Trent"),("NYKAA","Nykaa"),("DELHIVERY","Delhivery"),
    ("PAYTM","Paytm"),("ZOMATO","Zomato"),("NAUKRI","Info Edge"),
    ("INDHOTEL","Indian Hotels"),("LTIMINDTREE","LTIMindtree"),("MPHASIS","Mphasis"),
    ("PERSISTENT","Persistent Systems"),("COFORGE","Coforge"),("KPITTECH","KPIT Tech"),
    ("TATAELXSI","Tata Elxsi"),("OFSS","Oracle Financial"),
    # Banking & Finance
    ("BANKBARODA","Bank of Baroda"),("PNB","Punjab National Bank"),("CANBK","Canara Bank"),
    ("FEDERALBNK","Federal Bank"),("IDFCFIRSTB","IDFC First Bank"),("BANDHANBNK","Bandhan Bank"),
    ("RBLBANK","RBL Bank"),("AUBANK","AU Small Finance"),("CHOLAFIN","Cholamandalam"),
    ("BAJAJHLDNG","Bajaj Holdings"),("LICHSGFIN","LIC Housing"),("RECLTD","REC"),
    ("PFC","Power Finance"),("IRFC","IRFC"),("M&MFIN","M&M Financial"),
    # Auto
    ("TVSMOTOR","TVS Motor"),("ASHOKLEY","Ashok Leyland"),("ESCORTS","Escorts"),
    ("MOTHERSON","Motherson Sumi"),("BOSCHLTD","Bosch"),("BALKRISIND","Balkrishna Ind"),
    ("EXIDEIND","Exide Industries"),("AMARARAJA","Amara Raja"),
    # Energy & Infra
    ("ADANIGREEN","Adani Green"),("ADANIENSOL","Adani Energy Solutions"),("ADANIPOWER","Adani Power"),
    ("TATAPOWER","Tata Power"),("TORNTPOWER","Torrent Power"),("CESC","CESC"),
    ("NHPC","NHPC"),("SJVN","SJVN"),("IRCON","IRCON"),("NBCC","NBCC"),
    ("DLF","DLF"),("GODREJPROP","Godrej Properties"),("OBEROIRLTY","Oberoi Realty"),
    ("PRESTIGE","Prestige Estates"),("PHOENIXLTD","Phoenix Mills"),
    # IT
    ("LTTS","L&T Technology"),("ZENSARTECH","Zensar Tech"),("MASTEK","Mastek"),
    # Pharma
    ("ABBOTINDIA","Abbott India"),("PFIZER","Pfizer"),("GLAXO","GSK Pharma"),
    ("SANOFI","Sanofi India"),("NATCOPHARM","Natco Pharma"),("GRANULES","Granules India"),
    ("AJANTPHARM","Ajanta Pharma"),("LAURUSLABS","Laurus Labs"),
    # FMCG & Retail
    ("EMAMILTD","Emami"),("JYOTHYLAB","Jyothy Labs"),("RADICO","Radico Khaitan"),
    ("UNITDSPR","United Spirits"),("VBL","Varun Beverages"),
    ("TATACOMM","Tata Communications"),
    # Metals
    ("SAIL","Steel Authority"),("NMDC","NMDC"),("HINDCOPPER","Hindustan Copper"),
    ("NATIONALUM","National Aluminium"),("WELCORP","Welspun Corp"),
    ("RATNAMANI","Ratnamani Metals"),
    # Chemicals
    ("SRF","SRF"),("DEEPAKNITRITE","Deepak Nitrite"),("CLEAN","Clean Science"),("NAVINFLUOR","Navin Fluorine"),
    ("FLUOROCHEM","Gujarat Fluorochemicals"),
    # Cement
    ("JKCEMENT","JK Cement"),("RAMCOCEM","Ramco Cements"),("HEIDELBERG","HeidelbergCement"),
    ("BIRLACORPN","Birla Corp"),
    # Telecom & Media
    ("IDEA","Vodafone Idea"),("MTNL","MTNL"),("HFCL","HFCL"),
    # Misc
    ("IRCTC","IRCTC"),("CONCOR","Container Corp"),("GMRINFRA","GMR Infra"),
    ("AIAENG","AIA Engineering"),("GRINDWELL","Grindwell Norton"),
    ("CUMMINSIND","Cummins India"),("ABB","ABB India"),("HONAUT","Honeywell Auto"),
    ("SCHAEFFLER","Schaeffler India"),
]

# Deduplicate
_SEEN = set()
UNIVERSE = []
for sym, name in _UNIVERSE:
    if sym not in _SEEN:
        _SEEN.add(sym)
        UNIVERSE.append((sym, name))

ALL_SYMBOLS   = [s for s, _ in UNIVERSE]
NIFTY_50      = ALL_SYMBOLS[:50]   # first 50 entries = Nifty 50 index
_NAME_MAP     = {s: n for s, n in UNIVERSE}


def get_name(symbol: str) -> str:
    return _NAME_MAP.get(symbol, symbol)


def search(query: str, limit: int = 20) -> list[dict]:
    """Return up to `limit` stocks matching query (symbol or name prefix)."""
    q = query.strip().upper()
    results = []
    for sym, name in UNIVERSE:
        if q in sym or q in name.upper():
            results.append({"symbol": sym, "name": name})
        if len(results) >= limit:
            break
    return results


# ── Intraday screener ─────────────────────────────────────────────────────────

def _score_one(sym: str) -> dict | None:
    """Fetch today's 5-min data for sym and compute its intraday snapshot."""
    try:
        yf_sym = sym + ".NS"
        df = yf.Ticker(yf_sym).history(period="5d", interval="5m", auto_adjust=True)
        if df is None or df.empty:
            return None

        now_ist = pd.Timestamp.now(tz=_IST)
        today   = now_ist.date()
        df.index = df.index.tz_convert(_IST)
        today_df = df[df.index.date == today]
        if len(today_df) < 3:
            return None

        prev_df    = df[df.index.date < today]
        prev_close = float(prev_df["Close"].iloc[-1]) if not prev_df.empty else None

        # Previous full trading day's total volume (for a sane volume ratio)
        prev_day_vol = None
        if not prev_df.empty:
            prev_dates = sorted(set(prev_df.index.date))
            last_prev  = prev_dates[-1]
            prev_day_vol = float(prev_df[prev_df.index.date == last_prev]["Volume"].sum())

        snap = compute_snapshot(today_df, prev_close, prev_day_vol)
        if snap is None:
            return None

        snap["symbol"] = sym
        snap["name"]   = get_name(sym)
        return snap
    except Exception:
        return None


def screen_all(universe: list[str] = None) -> list[dict]:
    """
    Scan all NSE stocks in the universe and return every result sorted by
    momentum score (highest first).  n=None means return all.

    Default universe = ALL_SYMBOLS (~150 stocks, ~20-30s on a free-tier host).
    """
    syms = universe or ALL_SYMBOLS
    results = []
    with ThreadPoolExecutor(max_workers=15) as pool:
        futures = {pool.submit(_score_one, s): s for s in syms}
        for fut in as_completed(futures):
            r = fut.result()
            if r:
                results.append(r)
    results.sort(key=lambda x: x["score"], reverse=True)
    log.info("[Screener] Scanned %d / %d stocks", len(results), len(syms))
    return results


def screen_top(n: int = 20, universe: list[str] = None) -> list[dict]:
    """Convenience wrapper — return top-n by momentum score."""
    return screen_all(universe)[:n]
