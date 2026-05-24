#!/usr/bin/env python3
"""
start_public.py — Run StockBot with a public ngrok HTTPS tunnel.

Usage
-----
  python start_public.py                         # first run: will ask for authtoken
  python start_public.py --token <your_token>    # pass token directly
  python start_public.py --port 5000             # override port (default: config.WEB_PORT)

Your free authtoken is at: https://dashboard.ngrok.com/get-started/your-authtoken
Paste it once — it gets saved to ~/.ngrok2/ngrok.yml and reused automatically.
"""

import argparse
import os
import sys
import time

# ── Args ──────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="StockBot + ngrok public tunnel")
parser.add_argument("--token", default=os.getenv("NGROK_AUTH_TOKEN", ""), help="ngrok authtoken")
parser.add_argument("--port",  type=int, default=None,                      help="port override")
args = parser.parse_args()

# ── Check pyngrok ─────────────────────────────────────────────────────────────
try:
    from pyngrok import ngrok, conf, exception as ngrok_exc
except ImportError:
    print("Installing pyngrok…")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyngrok", "-q"])
    from pyngrok import ngrok, conf, exception as ngrok_exc

# ── Load app config ───────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
import config

PORT = args.port or config.WEB_PORT

# ── Auth token ────────────────────────────────────────────────────────────────
if args.token:
    ngrok.set_auth_token(args.token)
else:
    # Check if a token is already configured
    ngrok_config = conf.get_default()
    if not ngrok_config.auth_token:
        print("\n" + "=" * 60)
        print("  ngrok requires a free account to create tunnels.")
        print()
        print("  1. Sign up (free) at:  https://ngrok.com/signup")
        print("  2. Copy your authtoken from: https://dashboard.ngrok.com/get-started/your-authtoken")
        print("  3. Re-run with:  python start_public.py --token YOUR_TOKEN")
        print("     (token is saved — you only need to do this once)")
        print("=" * 60 + "\n")
        sys.exit(1)

# ── Start ngrok tunnel ────────────────────────────────────────────────────────
print(f"\nOpening ngrok tunnel → localhost:{PORT} …")
try:
    tunnel = ngrok.connect(PORT, "http")
    public_url = tunnel.public_url
    # ngrok returns http:// — upgrade to https://
    public_url_https = public_url.replace("http://", "https://")
except ngrok_exc.PyngrokNgrokError as e:
    print(f"\n[ERROR] ngrok failed: {e}")
    print("Check your authtoken or internet connection.")
    sys.exit(1)

print()
print("=" * 60)
print("  StockBot is PUBLIC!")
print()
print(f"  URL:   {public_url_https}")
print(f"  Local: http://127.0.0.1:{PORT}")
print()
print("  Open the URL on any device, any network.")
print("  The link stays active while this terminal is open.")
print("=" * 60 + "\n")

# ── Start Flask ───────────────────────────────────────────────────────────────
from database import db
db.init_db()

from analysis import ml_model
ml_model.retrain_if_stale()

# Start background threads (news + price refresh)
from app import _start_background_threads, app
_start_background_threads()

import logging
log = logging.getLogger("werkzeug")
log.setLevel(logging.WARNING)   # suppress per-request noise

print(f"Flask running on port {PORT} …\n")
app.run(
    host        = "127.0.0.1",
    port        = PORT,
    debug       = False,
    use_reloader= False,
)
