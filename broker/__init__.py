"""
Broker selection. The rest of the bot asks for "the broker client" and gets either KiteConnect (Zerodha) or the
Kite-shaped AngelClient — it never needs to know which.

    broker.load_token()          today's saved login token for the configured broker
    broker.make_client(token)    a ready client
"""
import hashlib
import json
import os
from datetime import datetime, timedelta

VERIFIED_MAX_AGE = timedelta(days=30)


def _client_fp(client_code: str) -> str:
    return hashlib.sha256(client_code.strip().upper().encode()).hexdigest()[:12]


def write_verified(client_code: str, now=None) -> None:
    """Called by scripts/verify_angel.py after a clean run during market hours."""
    import paths
    paths.ANGEL_VERIFIED_FILE.write_text(json.dumps({"at": (now or datetime.now()).isoformat(timespec="seconds"), "client": _client_fp(client_code)}))


def live_gate(broker: str, paper: bool, client_code: str, now=None):
    """
    (ok, reason). Real Angel orders are refused until scripts/verify_angel.py has passed, during market hours,
    for THIS Angel account within the last 30 days. The adapter was written without account access, so a live
    order must not be the first time its assumptions meet reality. Paper mode and Zerodha are never gated.
    """
    if broker != "angel" or paper:
        return True, ""
    import paths
    try:
        info = json.loads(paths.ANGEL_VERIFIED_FILE.read_text())
        at = datetime.fromisoformat(info["at"])
    except Exception:
        return False, "scripts/verify_angel.py has not been run (or did not pass) for this account"
    if info.get("client") != _client_fp(client_code):
        return False, "the verification on file is for a different Angel account"
    if (now or datetime.now()) - at > VERIFIED_MAX_AGE:
        return False, f"the last verification is from {at:%d %b}, over 30 days ago — run scripts/verify_angel.py again"
    return True, ""


def name() -> str:
    import config
    return getattr(config, "BROKER", "zerodha")


def _zerodha_paper() -> bool:
    import config, paths
    return name() == "zerodha" and bool(getattr(config, "BROKER_PAPER", False)) and paths.USER_DIR is not None


def load_token():
    if name() == "angel":
        from auth.angel_login import load_access_token
        return load_access_token()
    if _zerodha_paper():
        return load_owner_zerodha_token()
    from auth.kite_login import load_access_token
    return load_access_token()


def load_owner_zerodha_token():
    """
    A Zerodha PAPER instance has no login of its own: it reads the owner's daily token (written at 08:45 by
    trading-login.service) and uses it for market data only — PaperKite never lets an order through.
    """
    import paths
    f = paths.OWNER_TOKEN_FILE
    if not f.exists():
        raise FileNotFoundError(f"Paper mode reads the owner's Zerodha login at {f}, which does not exist yet.")
    data = json.loads(f.read_text())
    if data.get("date") != str(datetime.now().date()):
        raise ValueError("The owner's Zerodha access token is from a previous day (trading-login.service has not run yet today).")
    return data["access_token"]


def make_client(token):
    import config
    if name() == "angel":
        from broker.angel import AngelClient
        return AngelClient(api_key=os.environ["ANGEL_API_KEY"], access_token=token, paper=bool(getattr(config, "BROKER_PAPER", True)))
    from data.market_data import get_kite_client
    kite = get_kite_client(token)
    if _zerodha_paper():
        import paths
        from broker.angel_paper import PaperLedger
        from broker.kite_paper import PaperKite
        return PaperKite(kite, PaperLedger(paths.HOME / "paper_ledger.json"), config.NFO_EXCHANGE)
    return kite
