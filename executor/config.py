"""Executor settings, read from executor.env (KEY=value lines). Credentials never leave this machine."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOME = Path(os.environ.get("EXECUTOR_HOME", "/opt/executor"))


def _read(path: Path) -> dict:
    vals = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


@dataclass
class Settings:
    home: Path
    api_url: str = "https://159.223.87.53:8443"
    api_pin: str = ""                       # SHA-256 of the server's certificate
    api_key: str = ""
    broker: str = "paper"                   # zerodha | angel | paper (no broker at all)
    mode: str = "paper"                     # paper = never sends an order; live = real orders
    product: str = "NRML"
    disaster_sl_pct: float = 60.0           # broker stop on each short: entry × (1 + this %)
    max_lots_per_leg: int = 12
    stale_open_sec: int = 120               # never act on an entry/add older than this
    guard_grace_sec: int = 120              # spot past the stop this long with no exit instruction → exit
    entry_from: str = "09:15"
    entry_until: str = "14:55"
    poll_sec: float = 2.0
    creds: dict = field(default_factory=dict)

    @property
    def live(self) -> bool:
        return self.mode == "live"

    def problems(self) -> list:
        out = []
        if not self.api_key.startswith("tdx_"):
            out.append("EXEC_API_KEY is missing or not a Theta Desk key")
        if len(self.api_pin) != 64:
            out.append("EXEC_API_PIN must be the 64-character certificate fingerprint")
        if self.broker not in ("zerodha", "angel", "paper"):
            out.append("BROKER must be zerodha, angel or paper")
        if self.mode not in ("paper", "live"):
            out.append("MODE must be paper or live")
        if self.live and self.broker == "paper":
            out.append("MODE=live needs BROKER=zerodha or angel")
        need = {"zerodha": ("ZERODHA_USER_ID", "ZERODHA_PASSWORD", "ZERODHA_TOTP_SECRET", "KITE_API_KEY", "KITE_API_SECRET"),
                "angel": ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET")}.get(self.broker, ())
        out += [f"{k} is missing" for k in need if not self.creds.get(k)]
        return out


def load(home: Path | None = None) -> Settings:
    home = Path(home or DEFAULT_HOME)
    v = _read(home / "executor.env")
    num = lambda k, d, cast=float: cast(v[k]) if v.get(k) else d
    cred_keys = ("ZERODHA_USER_ID", "ZERODHA_PASSWORD", "ZERODHA_TOTP_SECRET", "KITE_API_KEY", "KITE_API_SECRET",
                 "ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET")
    return Settings(
        home=home, api_url=v.get("EXEC_API_URL", Settings.api_url).rstrip("/"), api_pin=v.get("EXEC_API_PIN", "").lower(),
        api_key=v.get("EXEC_API_KEY", ""), broker=v.get("BROKER", "paper").lower(), mode=v.get("MODE", "paper").lower(),
        product=v.get("PRODUCT", "NRML"), disaster_sl_pct=num("DISASTER_SL_PCT", 60.0),
        max_lots_per_leg=num("MAX_LOTS_PER_LEG", 12, int), stale_open_sec=num("STALE_OPEN_SEC", 120, int),
        guard_grace_sec=num("GUARD_GRACE_SEC", 120, int), poll_sec=num("POLL_SEC", 2.0),
        creds={k: v[k] for k in cred_keys if v.get(k)},
    )
