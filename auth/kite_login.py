"""
Run this script once each morning before 9:15 AM to generate a fresh access token.
Usage:  python auth/kite_login.py
"""

import os
import sys
import json
import webbrowser
from pathlib import Path
from kiteconnect import KiteConnect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import KITE_API_KEY, KITE_API_SECRET

TOKEN_FILE = Path(__file__).resolve().parent.parent / ".access_token"


def login():
    kite = KiteConnect(api_key=KITE_API_KEY)
    login_url = kite.login_url()

    print("\n── Zerodha KiteConnect Login ──────────────────────────")
    print(f"Opening login URL in your browser...")
    print(f"If browser does not open, visit:\n  {login_url}\n")
    webbrowser.open(login_url)

    redirect_url = input(
        "After login, paste the full redirect URL here (contains request_token=...):\n> "
    ).strip()

    # Extract request_token from redirect URL
    if "request_token=" not in redirect_url:
        print("ERROR: request_token not found in URL. Please try again.")
        sys.exit(1)

    request_token = redirect_url.split("request_token=")[1].split("&")[0]

    data = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
    access_token = data["access_token"]

    TOKEN_FILE.write_text(json.dumps({
        "access_token": access_token,
        "date": str(__import__("datetime").date.today())
    }))

    print(f"\nAccess token saved. Bot is ready to run.\n")
    return access_token


def load_access_token():
    if not TOKEN_FILE.exists():
        raise FileNotFoundError(
            "No access token found. Run:  python auth/kite_login.py"
        )

    data = json.loads(TOKEN_FILE.read_text())
    today = str(__import__("datetime").date.today())

    if data.get("date") != today:
        raise ValueError(
            "Access token is from a previous day. Run:  python auth/kite_login.py"
        )

    return data["access_token"]


if __name__ == "__main__":
    login()
