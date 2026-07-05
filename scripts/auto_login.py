"""
Automated headless Zerodha login for server deployment.
Uses credentials + TOTP — no browser required.

Requires in .env:
  ZERODHA_USER_ID      your Zerodha client ID (e.g. AB1234)
  ZERODHA_PASSWORD     your Zerodha password
  ZERODHA_TOTP_SECRET  base32 secret from Zerodha 2FA setup
  KITE_API_KEY         already set
  KITE_API_SECRET      already set

Run: python scripts/auto_login.py
"""

import json
import logging
import sys
from datetime import date
from pathlib import Path

import pyotp
import requests
from kiteconnect import KiteConnect
from dotenv import load_dotenv
import os

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
logger = logging.getLogger("auto_login")

TOKEN_FILE = Path(__file__).parent.parent / ".access_token"

KITE_BASE    = "https://kite.zerodha.com"
LOGIN_URL    = f"{KITE_BASE}/api/login"
TWOFA_URL    = f"{KITE_BASE}/api/twofa"
CONNECT_URL  = f"{KITE_BASE}/connect/login"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Referer":    "https://kite.zerodha.com/",
    "Origin":     "https://kite.zerodha.com",
    "Content-Type": "application/x-www-form-urlencoded",
}


def auto_login() -> str:
    user_id     = os.getenv("ZERODHA_USER_ID", "").strip()
    password    = os.getenv("ZERODHA_PASSWORD", "").strip()
    totp_secret = os.getenv("ZERODHA_TOTP_SECRET", "").strip()
    api_key     = os.getenv("KITE_API_KEY", "").strip()
    api_secret  = os.getenv("KITE_API_SECRET", "").strip()

    if not all([user_id, password, totp_secret, api_key, api_secret]):
        raise ValueError(
            "Missing credentials. Ensure ZERODHA_USER_ID, ZERODHA_PASSWORD, "
            "ZERODHA_TOTP_SECRET, KITE_API_KEY, KITE_API_SECRET are set in .env"
        )

    session = requests.Session()
    session.headers.update(HEADERS)

    # ── Step 1: Submit credentials ────────────────────────────────
    logger.info(f"Logging in as {user_id}...")
    r = session.post(LOGIN_URL, data={"user_id": user_id, "password": password}, timeout=15)
    r.raise_for_status()
    resp = r.json()

    if resp.get("status") != "success":
        raise Exception(f"Login failed: {resp.get('message', resp)}")

    request_id = resp["data"]["request_id"]
    logger.info("Credentials accepted. Submitting TOTP...")

    # ── Step 2: Submit TOTP ───────────────────────────────────────
    totp_value = pyotp.TOTP(totp_secret).now()
    r = session.post(TWOFA_URL, data={
        "user_id":     user_id,
        "request_id":  request_id,
        "twofa_value": totp_value,
        "twofa_type":  "totp",
        "skip_session": "",
    }, timeout=15)
    r.raise_for_status()
    resp = r.json()

    if resp.get("status") != "success":
        raise Exception(f"TOTP failed: {resp.get('message', resp)}")

    logger.info("TOTP accepted. Fetching request_token...")

    # ── Step 3: Trigger KiteConnect login flow ────────────────────
    # Follow redirects manually so we capture the request_token from the
    # Location header without needing the callback URL (localhost) to be reachable.
    connect_login = f"{CONNECT_URL}?api_key={api_key}&v=3"
    url = connect_login
    location = ""
    for _ in range(10):
        try:
            r = session.get(url, allow_redirects=False, timeout=15)
        except requests.exceptions.ConnectionError:
            break
        loc = r.headers.get("Location", "")
        if "request_token=" in loc:
            location = loc
            break
        if loc:
            url = loc
        else:
            location = r.url
            break

    if "request_token=" not in location:
        raise Exception(
            f"Could not extract request_token from redirect. "
            f"Last URL: {location[:200]}"
        )

    request_token = location.split("request_token=")[1].split("&")[0]
    logger.info(f"request_token obtained: {request_token[:12]}...")

    # ── Step 4: Generate access token ────────────────────────────
    kite         = KiteConnect(api_key=api_key)
    session_data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session_data["access_token"]

    TOKEN_FILE.write_text(json.dumps({
        "access_token": access_token,
        "date":         str(date.today()),
    }))

    logger.info(f"✓ Access token saved. User: {session_data.get('user_name')} | {session_data.get('email')}")
    return access_token


if __name__ == "__main__":
    try:
        auto_login()
    except Exception as e:
        logger.error(f"Auto-login failed: {e}")
        sys.exit(1)
