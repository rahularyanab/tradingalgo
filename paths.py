"""
Where this instance keeps its files.

Default (no ALGO_USER_DIR): exactly the layout the bot has always used, inside the repo
folder — so the owner's own instance is unchanged.

Multi-user (ALGO_USER_DIR=/opt/algo-users/<name>): everything that is per-person lives in
that folder — profile, secrets, broker token, logs, trade state, journal and the S/R
database — so several people's bots can share one server without touching each other.

This module imports nothing from the project (config imports it), so it is safe to import first.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

_raw = os.environ.get("ALGO_USER_DIR", "").strip()
USER_DIR = Path(_raw).expanduser().resolve() if _raw else None

HOME = USER_DIR or ROOT
LOGS_DIR = HOME / "logs"
BOT_LOG = LOGS_DIR / "bot.log"
STATE_FILE = LOGS_DIR / "algo_trade_state.json"
JOURNAL_FILE = LOGS_DIR / "trade_journal.csv"
TOKEN_FILE = HOME / ".access_token"
SR_DB_FILE = (USER_DIR / "data" / "sr_levels.json") if USER_DIR else (ROOT / "data" / "sr_levels.json")
ENV_FILE = (USER_DIR / ".env") if USER_DIR else None            # default instance: dotenv's normal lookup
PROFILE_FILE = (USER_DIR / "profile.toml") if USER_DIR else (ROOT / "profile.toml")
ONBOARD_FLAG = HOME / ".onboarded.json"
BROKER_OK_FLAG = HOME / ".broker_verified"
PAUSE_FILE = HOME / ".paused"                       # this instance only   (/pause, /resume)
GLOBAL_PAUSE_FILE = ROOT / "pause_all.flag"         # EVERY instance on this server, set by the owner (/pauseall, /resumeall)
ENTRY_FAILS_FILE = HOME / ".entry_fails"             # today's failed entry attempts (entry_guard.py)
ANGEL_VERIFIED_FILE = HOME / ".angel_verified"      # written by scripts/verify_angel.py when it passes in market hours


# Every secret the bot reads from the environment. In multi-user mode these are CLEARED before the
# person's own .env is read, so a key they left out can never fall back to something inherited from the
# owner's shell / service (which would send their alerts to the owner's chat or trade on the owner's key).
SECRET_ENV_NAMES = (
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "KITE_API_KEY", "KITE_API_SECRET", "ZERODHA_USER_ID", "ZERODHA_PASSWORD", "ZERODHA_TOTP_SECRET",
    "ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET",
)


def load_instance_env() -> None:
    """Load THIS instance's secrets: the owner's usual .env for the default instance, only the person's own .env otherwise."""
    from dotenv import load_dotenv
    if ENV_FILE is None:
        load_dotenv()
        return
    for k in SECRET_ENV_NAMES:
        os.environ.pop(k, None)
    load_dotenv(ENV_FILE, override=True)


def ensure_dirs() -> None:
    """Create the per-instance folders (no-op if they exist)."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    SR_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
