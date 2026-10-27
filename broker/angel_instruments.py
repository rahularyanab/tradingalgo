"""
Angel One instrument master -> Kite-shaped instrument records.

The format below was checked against Angel's real public scrip master (OpenAPIScripMaster.json):

    {"token": "51217", "symbol": "NIFTY27OCT2620150PE", "name": "NIFTY", "expiry": "27OCT2026",
     "strike": "2015000.000000", "lotsize": "65", "instrumenttype": "OPTIDX", "exch_seg": "NFO",
     "tick_size": "5.000000", "freeze_qty": "3511"}

Note: strike is stored x100, tick_size is in paise (5.0 = Rs 0.05), the symbol format differs from
Zerodha's (NIFTY + DDMMMYY + strike + CE/PE). The bot never builds symbols itself on this path — it
uses whatever tradingsymbol this module returns — so the two formats never meet.

Only NIFTY (index options/futures + the Nifty 50 index) is kept: the 37 MB master is reduced to a
small daily cache so scans never re-download it.
"""
import json
import logging
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# Kite-style keys the bot uses for the index -> (exchange, Angel trading symbol, Angel token)
INDEX_KEYS = {
    "NSE:NIFTY 50": ("NSE", "Nifty 50", "99926000"),
}
KITE_NIFTY_INDEX_TOKEN = 256265          # the constant in config.py (Zerodha's token) -> Angel index
ANGEL_NIFTY_INDEX = ("NSE", "99926000")

KEEP_NAMES = {"NIFTY"}


def parse_expiry(s: str):
    """'27OCT2026' -> date(2026, 10, 27); '' -> None."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s.title(), "%d%b%Y").date()
    except ValueError:
        return None


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def reduce_master(rows: list, names=KEEP_NAMES) -> list:
    """Keep only what the bot can trade: NIFTY index options/futures in NFO."""
    out = []
    for r in rows:
        if r.get("exch_seg") != "NFO" or r.get("name") not in names:
            continue
        if r.get("instrumenttype") not in ("OPTIDX", "FUTIDX"):
            continue
        out.append({k: r.get(k) for k in ("token", "symbol", "name", "expiry", "strike", "lotsize", "instrumenttype", "exch_seg", "tick_size", "freeze_qty")})
    return out


def to_kite(row: dict) -> dict | None:
    """One master row -> a Kite-shaped instrument dict (None if unusable)."""
    sym = row.get("symbol") or ""
    exp = parse_expiry(row.get("expiry"))
    if row.get("instrumenttype") == "FUTIDX":
        itype, strike = "FUT", 0.0
    elif sym.endswith("CE"):
        itype, strike = "CE", _f(row.get("strike")) / 100.0
    elif sym.endswith("PE"):
        itype, strike = "PE", _f(row.get("strike")) / 100.0
    else:
        return None
    if exp is None or not row.get("token"):
        return None
    try:
        tok = int(row["token"])
    except (TypeError, ValueError):
        return None
    return {
        "instrument_token": tok, "exchange_token": str(row["token"]), "tradingsymbol": sym, "name": row.get("name"),
        "expiry": exp, "strike": strike, "lot_size": int(_f(row.get("lotsize"), 0)), "instrument_type": itype,
        "segment": "NFO-OPT" if itype in ("CE", "PE") else "NFO-FUT", "exchange": "NFO",
        "tick_size": _f(row.get("tick_size")) / 100.0, "freeze_qty": int(_f(row.get("freeze_qty"), 0)),
    }


class InstrumentTable:
    """Daily-cached NIFTY instruments + symbol -> (exchange, token) lookup."""

    def __init__(self, cache_file: Path, http=None, today: date | None = None):
        self.cache_file = Path(cache_file)
        self.http = http
        self.today = today or date.today()
        self._rows: list | None = None
        self._by_symbol: dict = {}

    def _download(self) -> list:
        import requests
        http = self.http or requests
        logger.info("Downloading Angel instrument master (once a day)…")
        r = http.get(SCRIP_MASTER_URL, timeout=90)
        r.raise_for_status()
        return reduce_master(r.json())

    def load(self) -> list:
        if self._rows is not None:
            return self._rows
        rows = None
        try:
            c = json.loads(self.cache_file.read_text())
            if c.get("date") == self.today.isoformat():
                rows = c["rows"]
        except Exception:
            pass
        if rows is None:
            try:
                rows = self._download()
                self.cache_file.parent.mkdir(parents=True, exist_ok=True)
                self.cache_file.write_text(json.dumps({"date": self.today.isoformat(), "rows": rows}))
            except Exception as e:
                try:                                         # a stale cache beats no instruments at all
                    rows = json.loads(self.cache_file.read_text())["rows"]
                    logger.warning(f"Angel instrument download failed ({e}); using the cached list from an earlier day.")
                except Exception:
                    raise RuntimeError(f"Could not load Angel instruments: {e}")
        self._rows = rows
        self._by_symbol = {r["symbol"]: ("NFO", str(r["token"])) for r in rows}
        return rows

    def kite_instruments(self) -> list:
        return [k for k in (to_kite(r) for r in self.load()) if k]

    def resolve(self, key: str):
        """'NFO:NIFTY27OCT2620150PE' or 'NSE:NIFTY 50' -> (exchange, tradingsymbol, token) or None."""
        if key in INDEX_KEYS:
            return INDEX_KEYS[key]
        exch, _, sym = key.partition(":")
        if not sym:
            exch, sym = "NFO", key
        self.load()
        hit = self._by_symbol.get(sym)
        return (hit[0], sym, hit[1]) if hit else None
