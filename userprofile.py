"""
Per-user profile (profile.toml) -> bot settings.

A user edits ONE small file; everything strategy-related stays under the owner's control.

    [user]    name, owner                  owner = true lets this person pause every bot on the server
    [broker]  name                         "zerodha" (Angel One is added separately)
    [capital] total                        rupees allocated to this bot
    [risk]    max_daily_loss               optional, default 1% of capital, refused above 1.5%
              experience                   "new" (default) | "experienced"
              preferred_lots               optional starting lots for a STRONG signal
              allow_above_suggestion       default false
              allow_pyramiding             default true; false = never add size to a winning trade
    [sizing]  margin_per_lot, loss_per_lot optional planning inputs
    [overrides]                            OWNER ONLY: any setting that exists in config.py

Secrets (broker keys, Telegram token) are NOT in this file — they go in the sibling .env.

Not named "profile.py" on purpose: that would shadow Python's own `profile` module.
"""
import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path

import sizing
from sizing import ProfileError, Sizing

SUPPORTED_BROKERS = {"zerodha", "angel"}
PLANNED_BROKERS: set = set()

_SCHEMA = {
    "user":     {"name": str, "owner": bool},
    "broker":   {"name": str, "paper": bool},
    "capital":  {"total": (int, float)},
    "risk":     {"max_daily_loss": (int, float), "experience": str, "preferred_lots": int, "allow_above_suggestion": bool, "allow_pyramiding": bool},
    "sizing":   {"margin_per_lot": (int, float), "loss_per_lot": (int, float)},
    "overrides": None,                               # free-form, validated against config.py
}
_REQUIRED = [("capital", "total"), ("broker", "name")]


@dataclass
class Settings:
    name: str
    broker: str
    sizing: Sizing
    config_values: dict               # config constant -> value to apply
    profile_hash: str                 # changes whenever the file's content changes
    paper: bool = False               # simulated orders (Angel only)
    owner: bool = False               # may pause EVERY bot on the server (/pauseall)


def _type_name(t) -> str:
    t = t if isinstance(t, tuple) else (t,)
    if int in t and float in t:
        return "a number"
    names = {int: "a whole number", float: "a number", str: "text", bool: "true or false"}
    return " or ".join(dict.fromkeys(names[x] for x in t))


def _is(v, want) -> bool:
    """isinstance, but a TOML true/false is only accepted where a bool is wanted (True is an int in Python)."""
    wants = want if isinstance(want, tuple) else (want,)
    return bool in wants if isinstance(v, bool) else isinstance(v, wants)


def parse(text: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"profile.toml is not valid TOML: {e}")


def validate(data: dict) -> list:
    """Return a list of problems (empty = fine). Never raises."""
    errs = []
    for sec, body in data.items():
        if sec not in _SCHEMA:
            errs.append(f"Unknown section [{sec}]. Allowed: {', '.join('[' + s + ']' for s in _SCHEMA)}.")
            continue
        if not isinstance(body, dict):
            errs.append(f"[{sec}] must be a section with settings under it.")
            continue
        allowed = _SCHEMA[sec]
        if allowed is None:                          # [overrides]: free-form, checked against config.py later
            continue
        for k, v in body.items():
            if k not in allowed:
                errs.append(f"Unknown setting {k!r} in [{sec}]. Allowed: {', '.join(allowed)}.")
            elif not _is(v, allowed[k]):
                errs.append(f"{sec}.{k} must be {_type_name(allowed[k])} (got {v!r}).")
    for sec, k in _REQUIRED:
        body = data.get(sec)
        if not isinstance(body, dict) or k not in body:
            errs.append(f"Missing required setting: [{sec}] {k}.")
    broker = str(data.get("broker", {}).get("name", "")).strip().lower() if isinstance(data.get("broker"), dict) else ""
    if broker and broker not in SUPPORTED_BROKERS:
        if broker in PLANNED_BROKERS:
            errs.append(f'broker.name = "{broker}" is not available yet — only {", ".join(sorted(SUPPORTED_BROKERS))} works today.')
        else:
            errs.append(f'broker.name = "{broker}" is not supported. Choose one of: {", ".join(sorted(SUPPORTED_BROKERS))}.')
    body = data.get("broker") if isinstance(data.get("broker"), dict) else {}
    if body.get("paper") is True and broker and broker != "angel":
        errs.append("broker.paper = true is only available with Angel. A Zerodha profile always places real orders.")
    return errs


def _check_overrides(overrides: dict, config_ns: dict) -> dict:
    out, errs = {}, []
    for name, val in overrides.items():
        if not (name.isupper() and name in config_ns):
            errs.append(f"overrides.{name}: no such setting in config.py.")
            continue
        cur = config_ns[name]
        if isinstance(cur, bool) != isinstance(val, bool) or (not isinstance(cur, (int, float, str, bool))):
            errs.append(f"overrides.{name}: expected {type(cur).__name__}, got {val!r}.")
            continue
        if isinstance(cur, (int, float)) and not isinstance(cur, bool) and not isinstance(val, (int, float)):
            errs.append(f"overrides.{name}: expected a number, got {val!r}.")
            continue
        if isinstance(cur, str) and not isinstance(val, str):
            errs.append(f"overrides.{name}: expected text, got {val!r}.")
            continue
        out[name] = type(cur)(val) if isinstance(cur, (int, float)) and not isinstance(cur, bool) else val
    if errs:
        raise ProfileError("\n".join(errs))
    return out


def build(data: dict, config_ns: dict, raw_text: str = "") -> Settings:
    """Validate `data`, compute sizing, and return the settings to apply on top of config."""
    errs = validate(data)
    if errs:
        raise ProfileError("Your profile.toml needs fixing:\n  - " + "\n  - ".join(errs))
    risk, szg = data.get("risk", {}), data.get("sizing", {})
    s = sizing.compute(
        capital=data["capital"]["total"],
        max_daily_loss=risk.get("max_daily_loss"),
        preferred_lots=risk.get("preferred_lots"),
        experience=risk.get("experience", "new"),
        allow_above_suggestion=risk.get("allow_above_suggestion", False),
        margin_per_lot=szg.get("margin_per_lot", 90_000.0),
        loss_per_lot=szg.get("loss_per_lot"),
        max_start_lots=int(config_ns.get("MAX_START_LOTS", 3)),
        allow_pyramiding=risk.get("allow_pyramiding", True),
    )
    vals = {
        "DAILY_MAX_LOSS": int(s.daily_limit),
        "LOTS_STRONG": s.lots_strong,
        "LOTS_MODERATE": s.lots_moderate,
        "LOTS_STRANGLE": s.lots_strangle,
        "MAX_LOTS_DIRECTIONAL": s.max_lots_directional,
        "MAX_LOTS_STRANGLE_LEG": s.max_lots_strangle_leg,
        "PROTECTED_MAX_LOTS": s.protected_max_lots,
        "PYRAMID_STEP_LOTS": s.step_lots,
        "PROFIT_BOOK_STEP_LOTS": s.step_lots,
        **s.thresholds,
    }
    broker = str(data["broker"]["name"]).strip().lower()
    paper = bool(data["broker"].get("paper", broker == "angel"))     # Angel is paper-only until the owner says otherwise
    vals.update({"BROKER": broker, "BROKER_PAPER": paper})
    vals.update(_check_overrides(data.get("overrides", {}), config_ns))     # owner overrides win
    return Settings(
        name=str(data.get("user", {}).get("name", "")).strip() or "there",
        broker=broker, paper=paper,
        owner=bool(data.get("user", {}).get("owner", False)),
        sizing=s, config_values=vals,
        profile_hash=hashlib.sha256((raw_text or repr(sorted(data.items()))).encode()).hexdigest()[:16],
    )


def load(path: Path, config_ns: dict) -> Settings:
    path = Path(path)
    if not path.exists():
        raise ProfileError(f"No profile found at {path}. Create it from profile.example.toml (or run scripts/provision_user.py).")
    text = path.read_text()
    return build(parse(text), config_ns, text)


def apply_profile(config_ns: dict, path: Path | None = None, required: bool = False):
    """
    Called at the end of config.py. If a profile exists, overwrite the matching config
    constants (in place) and return the Settings; otherwise change nothing and return None.
    """
    import paths
    p = Path(path) if path else paths.PROFILE_FILE
    if not p.exists():
        if required:
            raise ProfileError(f"ALGO_USER_DIR is set but {p} does not exist.")
        return None
    st = load(p, config_ns)
    config_ns.update(st.config_values)
    return st


REQUIRED_SECRETS = {
    "zerodha": ["KITE_API_KEY", "KITE_API_SECRET", "ZERODHA_USER_ID", "ZERODHA_PASSWORD", "ZERODHA_TOTP_SECRET"],
    "angel": ["ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET"],
}
ALWAYS_REQUIRED_SECRETS = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]


def missing_secrets(broker: str, env: dict) -> list:
    """Names of required secrets that are empty/absent in `env`. Telegram is mandatory: alerts are how a user stays in control."""
    need = ALWAYS_REQUIRED_SECRETS + REQUIRED_SECRETS.get(broker, [])
    return [k for k in need if not str(env.get(k, "")).strip()]


def render_template(name: str, broker: str, capital: float, max_daily_loss=None, experience: str = "new", preferred_lots=None, paper=None,
                    allow_pyramiding: bool = True, owner: bool = False, loss_per_lot=None, max_failed_entries=None) -> str:
    """A documented profile.toml with the given values filled in."""
    ml = f"max_daily_loss = {int(max_daily_loss)}" if max_daily_loss else "# max_daily_loss = 10000       # optional; default is 1% of your capital, the most we accept is 1.5%"
    pl = f"preferred_lots = {int(preferred_lots)}" if preferred_lots else "# preferred_lots = 2          # optional; default is the suggested size"
    pp = ""
    if broker == "angel":
        pp = ("paper = " + ("false" if paper is False else "true") +
              "                        # true = real Angel market data but SIMULATED orders (no money). Set false ONLY after scripts/verify_angel.py passes\n")
    sz = (f"[sizing]\nloss_per_lot = {int(loss_per_lot)}                # what you accept ONE lot losing in a bad exit (default {int(sizing.PROFILES[experience][0])}). Lower = more risk than the bot assumes\n"
          if loss_per_lot else "")
    ov = (f"[overrides]                        # OWNER ONLY: any setting from config.py\nENTRY_FAIL_LIMIT = {int(max_failed_entries)}                 # after this many failed entry attempts in a day, stop entering until tomorrow\n"
          if max_failed_entries else "# [overrides]                      # OWNER ONLY: any setting from config.py, e.g.\n# PROFIT_LOCK_GIVEBACK_PCT = 0.35\n")
    po = "" if allow_pyramiding else "allow_pyramiding = false          # never add size to a winning trade: you hold exactly your starting lots\n"
    ow = "owner = true                       # may pause every bot on this server with /pauseall\n" if owner else ""
    return f"""# Your trading-bot profile. Edit the numbers, save, restart the bot. Secrets go in the .env file, NOT here.

[user]
name = "{name}"                     # used in Telegram messages
{ow}
[broker]
name = "{broker}"                   # "zerodha" or "angel"
{pp}
[capital]
total = {int(capital)}                    # rupees you allocate to this bot, in the broker account

[risk]
{ml}
experience = "{experience}"                # "new" = more cautious lot suggestion (use this for the first 1-2 months); "experienced" = less cautious
{pl}
# allow_above_suggestion = false   # keep false unless you accept losses above your daily limit
{po}
{sz if sz else "# [sizing]                         # planning inputs, normally leave out" + chr(10) + "# margin_per_lot = 90000" + chr(10) + "# loss_per_lot = 4500"}

{ov}
"""

