"""
Offline tests for the Bank Nifty / Sensex PAPER instances (indices.py, broker/kite_paper.py, [market] index).

    python3 tests/test_index_paper.py

Each index runs in its own child process (config is read once per process, from that instance's profile).
A fake Zerodha client serves prices; its order methods raise, so a test that reached the real order API fails
loudly. NSE's website is blocked inside the children. Nothing here talks to a broker or to Telegram.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra and not cond else ""))


PROFILE = """[user]
name = "Rahul"
[broker]
name = "zerodha"
paper = true
[market]
index = "{index}"
[capital]
total = 1000000
[risk]
max_daily_loss = 5000
experience = "new"
preferred_lots = 3
allow_above_suggestion = true
"""


# ───────────────────────────────────────── child: one index ─────────────────────────────────────────
def child(index: str):
    from datetime import date, datetime, timedelta
    home = Path(tempfile.mkdtemp()).resolve()
    (home / "profile.toml").write_text(PROFILE.format(index=index))
    (home / ".env").write_text("KITE_API_KEY=fake\n")          # no Telegram secrets: nothing can be posted
    os.environ["ALGO_USER_DIR"] = str(home)
    os.environ["TELEGRAM_BOT_TOKEN"] = os.environ["TELEGRAM_CHAT_ID"] = "leak"   # must be CLEARED by load_instance_env
    sys.path.insert(0, str(ROOT))

    import requests
    def _no_nse(*a, **k):
        raise AssertionError("NSE website must not be called for this index")
    requests.Session.get = _no_nse

    import config, paths
    out = {k: getattr(config, k) for k in ("INDEX", "INDEX_LABEL", "NFO_EXCHANGE", "INDEX_QUOTE_KEY", "NIFTY_LOT_SIZE", "STRIKE_STEP",
                                          "SL_BUFFER_POINTS", "HEDGE_PRICE_MIN", "LOTS_STRONG", "LOTS_MODERATE", "LOTS_STRANGLE",
                                          "MAX_LOTS_DIRECTIONAL", "MAX_LOTS_STRANGLE_LEG", "PYRAMID_STEP_LOTS", "PROFIT_BOOK_STEP_LOTS",
                                          "DAILY_MAX_LOSS", "PROFIT_LOCK_ARM_PNL", "PROFIT_BOOK_STEP1_PNL", "PROFIT_BOOK_STEP2_PNL",
                                          "DAY_PROFIT_ARM_PNL", "PROTECTED_MAX_LOTS", "BROKER", "BROKER_PAPER", "TELEGRAM_BOT_TOKEN")}
    out["paper"] = config.SETTINGS.paper
    lot, step, exch = config.NIFTY_LOT_SIZE, config.STRIKE_STEP, config.NFO_EXCHANGE
    name = config.UNDERLYING
    spot = 63700.0 if index == "BANKNIFTY" else 83900.0
    today = date.today()
    exp1 = today + timedelta(days=(3 - today.weekday()) % 7 or 7)
    exp2 = exp1 + timedelta(days=7)

    insts = []
    tok = 1000
    for e in (exp1, exp2):
        for k in range(int(spot // step) * step - 60 * step, int(spot // step) * step + 61 * step, step):
            for t in ("CE", "PE"):
                tok += 1
                insts.append({"instrument_token": tok, "tradingsymbol": f"{name}{e:%y%b}{k}{t}".upper(), "name": name, "expiry": e,
                              "strike": float(k), "instrument_type": t, "lot_size": lot, "exchange": exch, "segment": f"{exch}-OPT"})
    insts.append({"instrument_token": 9, "tradingsymbol": "NIFTY26DEC26000CE", "name": "NIFTY", "expiry": exp1, "strike": 26000.0,
                  "instrument_type": "CE", "lot_size": 65, "exchange": exch, "segment": f"{exch}-OPT"})   # another index: ignored
    by_sym = {f"{exch}:{i['tradingsymbol']}": i for i in insts}

    def price(key):
        if key == config.INDEX_QUOTE_KEY:
            return spot
        i = by_sym[key]
        d = (i["strike"] - spot) if i["instrument_type"] == "CE" else (spot - i["strike"])
        return round(max(0.5, 600 * 0.999 ** max(d, 0) + max(-d, 0)), 2)    # smooth premium curve: OTM decays, ITM adds intrinsic

    calls = {"instruments": [], "order_api": 0, "ltp_keys": set()}

    class FakeKite:
        VARIETY_REGULAR, ORDER_TYPE_LIMIT, ORDER_TYPE_MARKET = "regular", "LIMIT", "MARKET"
        TRANSACTION_TYPE_BUY, TRANSACTION_TYPE_SELL, PRODUCT_NRML, EXCHANGE_NFO = "BUY", "SELL", "NRML", "NFO"

        def instruments(self, exchange=None):
            calls["instruments"].append(exchange)
            return insts

        def ltp(self, keys):
            keys = [keys] if isinstance(keys, str) else keys
            calls["ltp_keys"].update(keys)
            return {k: {"last_price": price(k)} for k in keys if k in by_sym or k == config.INDEX_QUOTE_KEY}

        def quote(self, keys):
            keys = [keys] if isinstance(keys, str) else keys
            return {k: {"last_price": price(k), "oi": 1000 + (hash(k) % 5000), "volume": 10} for k in keys}

        def _real_order(self, *a, **k):
            calls["order_api"] += 1
            raise AssertionError("a real order API was called")
        place_order = modify_order = cancel_order = positions = orders = margins = _real_order

    import data.market_data as md
    md.get_kite_client = lambda token: FakeKite()
    paths.OWNER_TOKEN_FILE = home / "owner_token"
    import broker
    try:
        broker.load_token()
        out["token_missing_raises"] = False
    except FileNotFoundError:
        out["token_missing_raises"] = True
    paths.OWNER_TOKEN_FILE.write_text(json.dumps({"access_token": "abc", "date": str(today - timedelta(days=1))}))
    try:
        broker.load_token()
        out["stale_token_raises"] = False
    except ValueError:
        out["stale_token_raises"] = True
    paths.OWNER_TOKEN_FILE.write_text(json.dumps({"access_token": "abc", "date": str(today)}))
    out["token"] = broker.load_token()
    kite = broker.make_client(out["token"])
    out["client"] = type(kite).__name__

    from data.option_chain import fetch_option_chain
    oc = fetch_option_chain(kite=kite)
    out["chain_ok"] = oc is not None
    if oc:
        strikes = sorted(oc.call_data)
        out["chain_steps"] = sorted({b - a for a, b in zip(strikes, strikes[1:])})
        out["chain_span"] = (strikes[-1] - strikes[0]) / 2
        out["chain_symbol"] = next(iter(oc.call_data.values())).symbol
        out["chain_expiry"] = oc.weekly_expiry_date
        out["exp1"] = exp1.strftime("%d-%b-%Y")
        out["instruments_exchange"] = calls["instruments"]
        from strategy.option_signal import analyse_option_signal
        sig = analyse_option_signal(oc, spot + 4 * step, spot - 4 * step)
        real = {i["tradingsymbol"] for i in insts}
        out["sell_symbols_ok"] = bool(sig.call_symbol in real and sig.put_symbol in real)
        out["sold_ltps"] = (sig.call_ltp, sig.put_ltp)
        out["hedges"] = (sig.hedge_call_ltp, sig.hedge_put_ltp)

    # orders: through the bot's own order helpers, into the paper ledger
    from execution.order_manager import place_sell_order, place_buy_order
    sym = f"{name}{exp1:%y%b}{int(spot // step) * step + 5 * step}CE".upper()
    ltp = price(f"{exch}:{sym}")
    r = place_sell_order(kite, sym, 3 * lot, price=ltp)
    out["sell_ok"] = r.success
    pos = kite.positions()["net"]
    out["net_qty"] = pos[0]["quantity"] if pos else None
    out["pos_exchange"] = pos[0]["exchange"] if pos else None
    r2 = place_buy_order(kite, sym, 3 * lot, price=ltp)
    out["buy_ok"] = r2.success
    out["flat_after"] = [p["quantity"] for p in kite.positions()["net"]]
    out["orders_complete"] = [o["status"] for o in kite.orders()]
    out["order_api_calls"] = calls["order_api"]
    try:
        kite.margins()
        out["margins_blocked"] = False
    except AttributeError:
        out["margins_blocked"] = True
    out["ledger_file"] = (home / "paper_ledger.json").exists()

    import main
    out["main_exchange"] = main.NFO_EXCHANGE
    out["sr_file_in_instance"] = str(paths.SR_DB_FILE).startswith(str(home))
    from data import option_chain as ocm
    out["expiry_today_flag"] = main._is_expiry_day(datetime.combine(exp1, datetime.min.time()).replace(hour=11))
    out["not_expiry_flag"] = main._is_expiry_day(datetime.combine(exp1 - timedelta(days=1), datetime.min.time()).replace(hour=11)) \
        if ocm._kite_instruments_cache.get("date") == (exp1 - timedelta(days=1)) else None
    import notifications.telegram_bot as tb
    out["prefix"] = tb._MSG_PREFIX
    print("RESULT " + json.dumps(out, default=str))


# ───────────────────────────────────────── parent ─────────────────────────────────────────
def run_child(index):
    env = {k: v for k, v in os.environ.items() if k != "ALGO_USER_DIR"}
    p = subprocess.run([sys.executable, __file__, "--child", index], capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=120)
    line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT ")), None)
    if line is None:
        print(p.stdout[-3000:], p.stderr[-3000:])
        return None
    return json.loads(line[7:])


def main():
    sys.path.insert(0, str(ROOT))
    os.environ.pop("ALGO_USER_DIR", None)
    os.environ["TELEGRAM_BOT_TOKEN"] = os.environ["TELEGRAM_CHAT_ID"] = ""
    import config, indices, userprofile

    print("[1] the owner's Nifty bot is unchanged")
    nv = indices.values_for("NIFTY")
    diff = {k: (getattr(config, k), v) for k, v in nv.items() if getattr(config, k) != v}
    check("every index-dependent config default equals the NIFTY table (no profile = Nifty, as before)", not diff, str(diff))
    check("owner defaults: lots 6/4/4, NFO, lot 65", (config.LOTS_STRONG, config.LOTS_MODERATE, config.LOTS_STRANGLE, config.NFO_EXCHANGE, config.NIFTY_LOT_SIZE) == (6, 4, 4, "NFO", 65))
    check("owner instance is never paper", config.BROKER_PAPER is False and config.SETTINGS is None)

    print("\n[2] profile rules")
    live_bn = PROFILE.format(index="BANKNIFTY").replace("paper = true\n", "")
    e = None
    try:
        userprofile.build(userprofile.parse(live_bn), vars(config), live_bn)
    except userprofile.ProfileError as x:
        e = str(x)
    check("Bank Nifty without paper = true is refused (paper only)", e and "paper mode only" in e, str(e))
    bad = PROFILE.format(index="FINNIFTY")
    try:
        userprofile.build(userprofile.parse(bad), vars(config), bad); e = None
    except userprofile.ProfileError as x:
        e = str(x)
    check("an unknown index is refused with the list of choices", e and "BANKNIFTY" in e and "SENSEX" in e, str(e))
    check("Zerodha paper needs only Telegram + KITE_API_KEY",
          userprofile.missing_secrets("zerodha", {}, paper=True) == ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "KITE_API_KEY"])
    check("live Zerodha still needs its full login secrets", "ZERODHA_TOTP_SECRET" in userprofile.missing_secrets("zerodha", {}))

    expect = {
        "BANKNIFTY": dict(label="BANK NIFTY", exch="NFO", quote="NSE:NIFTY BANK", lot=30, step=100, sl=125),
        "SENSEX":    dict(label="SENSEX", exch="BFO", quote="BSE:SENSEX", lot=20, step=100, sl=160),
    }
    for idx, x in expect.items():
        print(f"\n[3] {idx} paper instance (child process)")
        r = run_child(idx)
        check(f"{idx}: child ran", r is not None)
        if r is None:
            continue
        check(f"{idx}: contract details ({x['exch']}, {x['quote']}, lot {x['lot']}, step {x['step']})",
              (r["NFO_EXCHANGE"], r["INDEX_QUOTE_KEY"], r["NIFTY_LOT_SIZE"], r["STRIKE_STEP"], r["INDEX_LABEL"]) == (x["exch"], x["quote"], x["lot"], x["step"], x["label"]), str(r))
        check(f"{idx}: SL buffer scaled to {x['sl']} pts, hedge band scaled", r["SL_BUFFER_POINTS"] == x["sl"] and r["HEDGE_PRICE_MIN"] > 3)
        check(f"{idx}: lots 3/2/2, pyramid caps 6/4, steps 2", (r["LOTS_STRONG"], r["LOTS_MODERATE"], r["LOTS_STRANGLE"], r["MAX_LOTS_DIRECTIONAL"],
              r["MAX_LOTS_STRANGLE_LEG"], r["PYRAMID_STEP_LOTS"], r["PROFIT_BOOK_STEP_LOTS"], r["PROTECTED_MAX_LOTS"]) == (3, 2, 2, 6, 4, 2, 2, 2), str(r))
        check(f"{idx}: ₹ limits halved — loss 5000, lock 1500, booking 5000/7500, day-profit 2500",
              (r["DAILY_MAX_LOSS"], r["PROFIT_LOCK_ARM_PNL"], r["PROFIT_BOOK_STEP1_PNL"], r["PROFIT_BOOK_STEP2_PNL"], r["DAY_PROFIT_ARM_PNL"]) == (5000, 1500, 5000, 7500, 2500), str(r))
        check(f"{idx}: paper on Zerodha, PaperKite client", r["BROKER"] == "zerodha" and r["BROKER_PAPER"] is True and r["paper"] is True and r["client"] == "PaperKite")
        check(f"{idx}: inherited Telegram secrets cleared (instance .env only)", r["TELEGRAM_BOT_TOKEN"] == "")
        check(f"{idx}: owner token — missing / stale refused, today's used", r["token_missing_raises"] and r["stale_token_raises"] and r["token"] == "abc")
        check(f"{idx}: option chain built from the broker (NSE never called)", r["chain_ok"])
        if r["chain_ok"]:
            check(f"{idx}: chain strikes every {x['step']} pts, real {x['exch']} symbols of the nearest expiry",
                  r["chain_steps"] == [x["step"]] and r["chain_symbol"].startswith(idx) and r["chain_expiry"] == r["exp1"], str(r))
            check(f"{idx}: instruments loaded from {x['exch']}", set(r["instruments_exchange"]) == {x["exch"]}, str(r["instruments_exchange"]))
            check(f"{idx}: chain covers the scaled width", r["chain_span"] >= 3000, str(r["chain_span"]))
            check(f"{idx}: strike picker returns real {x['exch']} symbols at a premium >= the scaled minimum",
                  r["sell_symbols_ok"] and all(p and p >= 25 for p in r["sold_ltps"]), str((r["sell_symbols_ok"], r["sold_ltps"], r["hedges"])))
        check(f"{idx}: SELL then BUY through the bot's order helpers fill in the paper ledger",
              r["sell_ok"] and r["net_qty"] == -3 * x["lot"] and r["buy_ok"] and r["flat_after"] == [0] and r["orders_complete"] == ["COMPLETE", "COMPLETE"], str(r))
        check(f"{idx}: positions carry the {x['exch']} exchange", r["pos_exchange"] == x["exch"])
        check(f"{idx}: the real order API was never called; margins() blocked", r["order_api_calls"] == 0 and r["margins_blocked"])
        check(f"{idx}: ledger + S/R database live in the instance folder", r["ledger_file"] and r["sr_file_in_instance"])
        check(f"{idx}: main uses {x['exch']}; expiry day comes from the contract list", r["main_exchange"] == x["exch"] and r["expiry_today_flag"] is True, str(r))
        check(f"{idx}: every alert is stamped PAPER · {x['label']}", r["prefix"].startswith("📝 *PAPER · " + x["label"]), r["prefix"])

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        child(sys.argv[2])
    else:
        main()
