"""
Which index this instance trades: NIFTY (default — the owner's bot, unchanged), BANKNIFTY or SENSEX.

A profile picks one with

    [market]
    index = "BANKNIFTY"

and `values_for()` returns every config constant that depends on the index. The strategy rules are the
same for every index; only the contract details differ, and every setting measured in index POINTS or in
option-premium RUPEES is scaled by the index's size relative to Nifty, so a "50-point SL buffer" on Nifty
(about 0.2% of the index) stays about 0.2% on Bank Nifty and Sensex.

Pure data + arithmetic: imports nothing from the project.
"""

# Nifty values of everything that scales with the size of the index (points or premium rupees).
NIFTY_POINT_SETTINGS = {
    "CLUSTER_BAND": 75,           # S/R pivots within this many points form one level
    "SL_BUFFER_POINTS": 50,       # SL = trendline level +/- this
    "STRANGLE_SL_BUFFER": 50,
    "ROLL_THRESHOLD_PTS": 100,    # OI-wall shift that triggers a roll
    "DASH_SL_WARN_PTS": 30,       # "AT RISK" / "within N pts of SL" warnings
    "CHAIN_HALF_WIDTH_PTS": 1250, # option chain covers spot +/- this
}
NIFTY_PREMIUM_SETTINGS = {
    "HEDGE_PRICE_MIN": 3.0,
    "HEDGE_PRICE_MAX": 8.0,
    "HEDGE_PRICE_TARGET": 5.5,
    "LOW_PREMIUM_THRESHOLD": 50,  # below this, look at the next expiry
    "TRADEABLE_LTP_MIN": 30.0,    # preferred minimum premium to sell
    "MIN_SELL_LTP": 10.0,         # never sell below this premium
}

SPECS = {
    "NIFTY": {
        "label": "NIFTY", "index_symbol": "NIFTY 50", "index_exchange": "NSE", "index_token": 256265,
        "deriv_exchange": "NFO", "lot_size": 65, "strike_step": 50, "expiry_weekday": 1,   # weekly, Tuesday
        "scale": 1.0, "nse_option_chain": True,
    },
    "BANKNIFTY": {
        # Monthly contracts only (no weekly Bank Nifty options since Nov 2024). Last Tuesday of the month,
        # moved earlier for holidays (e.g. Mon 25 Jan 2027) — the expiry date comes from the instrument list.
        "label": "BANK NIFTY", "index_symbol": "NIFTY BANK", "index_exchange": "NSE", "index_token": 260105,
        "deriv_exchange": "NFO", "lot_size": 30, "strike_step": 100, "expiry_weekday": 1,
        "scale": 2.5, "nse_option_chain": False,   # ~63,700 vs Nifty ~25,800 (Nov 2026)
    },
    "SENSEX": {
        "label": "SENSEX", "index_symbol": "SENSEX", "index_exchange": "BSE", "index_token": 265,
        "deriv_exchange": "BFO", "lot_size": 20, "strike_step": 100, "expiry_weekday": 3,  # weekly, Thursday
        "scale": 3.25, "nse_option_chain": False,  # ~83,900 vs Nifty ~25,800 (Nov 2026); BSE, not on NSE's site
    },
}


def _round_to(x: float, step: float) -> float:
    return round(round(x / step) * step, 2)


def values_for(index: str) -> dict:
    """Config constant -> value for `index`. For NIFTY these equal config.py's own values."""
    key = str(index).strip().upper().replace(" ", "")
    if key not in SPECS:
        raise ValueError(f'market.index = "{index}" is not supported. Choose one of: {", ".join(SPECS)}.')
    s = SPECS[key]
    out = {
        "INDEX": key,
        "INDEX_LABEL": s["label"],
        "UNDERLYING": key,
        "NIFTY_SYMBOL": s["index_symbol"],
        "NIFTY_EXCHANGE": s["index_exchange"],
        "NIFTY_INSTRUMENT_TOKEN": s["index_token"],
        "NFO_EXCHANGE": s["deriv_exchange"],
        "INDEX_QUOTE_KEY": f'{s["index_exchange"]}:{s["index_symbol"]}',
        "NIFTY_LOT_SIZE": s["lot_size"],
        "STRIKE_STEP": s["strike_step"],
        "EXPIRY_WEEKDAY": s["expiry_weekday"],
        "USE_NSE_OPTION_CHAIN": s["nse_option_chain"],
    }
    k = s["scale"]
    for name, v in NIFTY_POINT_SETTINGS.items():
        out[name] = int(_round_to(v * k, 5))
    for name, v in NIFTY_PREMIUM_SETTINGS.items():
        out[name] = _round_to(v * k, 0.5) if isinstance(v, float) else int(_round_to(v * k, 5))
    return out
