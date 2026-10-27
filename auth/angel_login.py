"""
Angel One login. Unlike Zerodha this is a plain API call (client code + PIN + a TOTP code) — no browser, no
redirect — so the daily refresh can run unattended.

Needs in the person's .env:
    ANGEL_API_KEY        the SmartAPI app's API key
    ANGEL_CLIENT_CODE    their Angel client id
    ANGEL_PASSWORD       their login PIN
    ANGEL_TOTP_SECRET    the base32 secret shown when enabling TOTP on their Angel account

Run:  ALGO_USER_DIR=/opt/algo-users/<name> python3 scripts/login.py
"""
import json
import logging
import os
import stat
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from paths import TOKEN_FILE            # per-instance (see paths.py)

logger = logging.getLogger("angel_login")


def login(api_key, client_code, password, totp_secret, client=None, token_file=None) -> str:
    """Log in, save today's token, return the access token."""
    import pyotp
    from broker.angel import AngelClient, AngelError
    client = client or AngelClient(api_key, None, paper=False)
    body = client._call("login", {"clientcode": client_code, "password": password, "totp": pyotp.TOTP(totp_secret).now()}, auth=False)
    d = body.get("data") or {}
    jwt = d.get("jwtToken")
    if not jwt:
        raise AngelError(f"Angel login returned no token: {body.get('message')}")
    f = Path(token_file or TOKEN_FILE)
    f.write_text(json.dumps({"access_token": jwt, "refresh_token": d.get("refreshToken"), "feed_token": d.get("feedToken"),
                             "client_code": client_code, "date": str(date.today())}))
    os.chmod(f, stat.S_IRUSR | stat.S_IWUSR)
    return jwt


def load_access_token(token_file=None) -> str:
    f = Path(token_file or TOKEN_FILE)
    if not f.exists():
        raise FileNotFoundError("No Angel access token found. Run:  python scripts/login.py")
    d = json.loads(f.read_text())
    if d.get("date") != str(date.today()):
        raise ValueError("Angel access token is from a previous day. Run:  python scripts/login.py")
    return d["access_token"]


def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    need = ["ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET"]
    missing = [k for k in need if not os.environ.get(k, "").strip()]
    if missing:
        logger.error(f"Missing in .env: {', '.join(missing)}")
        return 2
    try:
        login(os.environ["ANGEL_API_KEY"], os.environ["ANGEL_CLIENT_CODE"], os.environ["ANGEL_PASSWORD"], os.environ["ANGEL_TOTP_SECRET"])
    except Exception as e:
        logger.error(f"Angel login failed: {e}")
        return 1
    logger.info("✓ Angel access token saved.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
