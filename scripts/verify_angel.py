"""
Check the Angel adapter against a REAL Angel account. Nothing here trades unless you add --test-order.

    ALGO_USER_DIR=/opt/algo-users/priya python3 scripts/verify_angel.py            # read-only
    ALGO_USER_DIR=/opt/algo-users/priya python3 scripts/verify_angel.py --test-order

Run it DURING MARKET HOURS (09:15-15:30 IST, Mon-Fri) — open interest and volume are only meaningful then.

It exists because the adapter was written without access to an Angel account. Every ✅ below turns an
assumption into a fact; every ❌ says exactly what to fix. Angel stays in PAPER mode (simulated orders)
until this passes and the owner switches `paper = false` in the profile.

Read-only checks: login, profile, funds, instruments, Nifty price, quote batching + open interest,
15-minute candles, positions, order book, and the bot's own option-chain builder end to end.

--test-order (optional, asks you to type YES): places ONE limit BUY of 1 lot on a far out-of-the-money Nifty
put at ₹0.05, so it should never fill; then checks it shows in the order book, modifies it, cancels it and
confirms the cancel. If it ever DID fill you would own 1 lot worth a few rupees — the script tells you.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, WARN, BAD = "✅", "⚠️ ", "❌"


def run(client, *, test_order=False, confirm=input, say=print, now=None) -> list:
    res = []
    now = now or datetime.now()

    def rec(status, label, detail=""):
        res.append((status, label, detail))
        say(f" {status} {label}" + (f" — {detail}" if detail else ""))

    market_open = now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 30)
    if not market_open:
        rec(WARN, "market is closed", "open-interest / volume / fill checks below are not conclusive — run again 09:15-15:30 IST")

    # ── account ──
    try:
        p = client.profile()
        cc = str(p.get("clientcode", "?"))
        rec(OK, "profile", f"client {cc[:2]}***{cc[-2:]}  exchanges={p.get('exchanges', '?')}")
    except Exception as e:
        rec(BAD, "profile", str(e))
    try:
        r = client.rms()
        rec(OK, "funds", "keys: " + ", ".join(sorted(r.keys())[:8]) + f"  | net={r.get('net')} available cash={r.get('availablecash')}")
    except Exception as e:
        rec(BAD, "funds (getRMS)", str(e))

    # ── instruments ──
    try:
        ins = client.instruments("NFO")
        opts = [i for i in ins if i["instrument_type"] in ("CE", "PE")]
        exps = sorted({i["expiry"] for i in opts})
        lots = {i["lot_size"] for i in opts}
        rec(OK if opts and lots == {65} else WARN, "instruments", f"{len(opts)} NIFTY options, next expiries {[e.isoformat() for e in exps[:3]]}, lot size {sorted(lots)}")
    except Exception as e:
        rec(BAD, "instruments", str(e))
        return res

    # ── price ──
    spot = None
    try:
        spot = client.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"]
        rec(OK if spot > 1000 else BAD, "Nifty 50 price", f"{spot}")
    except Exception as e:
        rec(BAD, "Nifty 50 price", str(e))

    # ── candles ──
    try:
        c = client.historical_data(256265, now - timedelta(days=5), now, "15minute")
        rec(OK if c and c[-1]["date"].tzinfo else WARN, "15-minute candles", f"{len(c)} bars, last {c[-1]['date']} close {c[-1]['close']}" if c else "no bars returned")
    except Exception as e:
        rec(BAD, "15-minute candles", str(e))

    # ── quotes: batching and open interest ──
    if spot:
        today = now.date()
        exp = next((e for e in exps if e > today), exps[0])
        atm = round(spot / 50) * 50
        pool = [i for i in opts if i["expiry"] == exp and i["instrument_type"] == "CE" and abs(i["strike"] - atm) <= 1500]
        pool.sort(key=lambda i: abs(i["strike"] - atm))
        best, oi_seen, oi_n = 0, 0, 0
        for size in (1, 5, 25, 50, 100):
            sel = pool[:size]
            if len(sel) < size:
                break
            t0 = time.time()
            try:
                body = client._call("quote", {"mode": "FULL", "exchangeTokens": {"NFO": [i["exchange_token"] for i in sel]}})
            except Exception as e:
                rec(WARN, f"quote batch of {size}", f"failed: {e}")
                break
            d = body.get("data") or {}
            fetched = d.get("fetched") or []
            dt = time.time() - t0
            if len(fetched) == size:
                best = size
                oi_n = sum(1 for e in fetched if float(e.get("opnInterest") or 0) > 0)
                oi_seen = len(fetched)
                rec(OK, f"quote batch of {size}", f"all {size} returned in {dt:.1f}s")
            else:
                rec(WARN, f"quote batch of {size}", f"only {len(fetched)} of {size} returned ({len(d.get('unfetched') or [])} reported unfetched)")
                break
        if best:
            rec(OK, "recommended ANGEL_QUOTE_BATCH", str(best) + ("  (the adapter also adapts by itself)" if best < 50 else ""))
        else:
            rec(BAD, "quote batching", "not even a single-token quote worked")
        if oi_seen:
            rec(OK if oi_n else (BAD if market_open else WARN), "option open interest", f"{oi_n} of {oi_seen} option quotes carry OI > 0" + ("" if oi_n else "  — without OI the bot's PCR / walls / writing signals cannot work"))

    # ── positions and orders (mapped to the bot's shape) ──
    try:
        net = client.positions()["net"]
        rec(OK, "positions", f"{len(net)} open/closed today; shape ok" if all({"tradingsymbol", "quantity"} <= set(x) for x in net) else "unexpected shape")
        for x in net[:3]:
            rec(OK, "  position sample", f"{x['tradingsymbol']} net={x['quantity']} (buy {x.get('buy_quantity')}, sell {x.get('sell_quantity')})")
    except Exception as e:
        rec(BAD, "positions", str(e))
    try:
        od = client.orders()
        need = {"order_id", "tradingsymbol", "transaction_type", "status", "quantity", "filled_quantity", "order_timestamp"}
        rec(OK if all(need <= set(o) for o in od) else BAD, "order book", f"{len(od)} orders today" + ("" if all(need <= set(o) for o in od) else "  — missing fields"))
        if od:
            raw = client._raw_orders()[0]
            rec(OK, "  raw order fields seen", ", ".join(sorted(raw.keys())))
            rec(OK, "  status values today", ", ".join(sorted({str(o.get('orderstatus') or o.get('status')) for o in client._raw_orders()})))
    except Exception as e:
        rec(BAD, "order book", str(e))

    # ── the bot's real option-chain builder ──
    try:
        from data import option_chain as oc
        t0 = time.time()
        chain = oc._fetch_option_chain_from_kite(client)
        if chain is None:
            rec(BAD, "option chain (bot's own builder)", "returned nothing — see the log above")
        else:
            rec(OK, "option chain (bot's own builder)", f"spot {chain.spot_price}  PCR {chain.pcr}  walls {chain.call_wall}/{chain.put_wall}  built in {time.time() - t0:.0f}s "
                f"({client.stats['calls']} API calls so far)")
            if time.time() - t0 > 60:
                rec(WARN, "  chain build is slow", "over a minute per scan blocks the 60-second stop-loss guard; tell the owner")
    except Exception as e:
        rec(BAD, "option chain (bot's own builder)", str(e))

    # ── optional: one order through its whole life ──
    if test_order:
        if not spot:
            rec(BAD, "order test", "skipped — no Nifty price")
        else:
            far = [i for i in opts if i["expiry"] == next((e for e in exps if e > now.date()), exps[0]) and i["instrument_type"] == "PE" and i["strike"] <= spot - 1500]
            far.sort(key=lambda i: -i["strike"])
            if not far:
                rec(BAD, "order test", "no far out-of-the-money put found")
            else:
                inst = far[0]
                say(f"\n   About to place: BUY 1 lot ({inst['lot_size']}) {inst['tradingsymbol']} LIMIT ₹0.05 — far out of the money, should NOT fill.")
                if confirm("   Type YES to continue: ").strip() != "YES":
                    rec(WARN, "order test", "cancelled by you")
                    return res
                try:
                    oid = client.place_order(variety="regular", exchange="NFO", tradingsymbol=inst["tradingsymbol"], transaction_type="BUY",
                                             quantity=inst["lot_size"], product="NRML", order_type="LIMIT", price=0.05)
                    rec(OK, "order placed", f"id {oid}")
                    time.sleep(1.5)
                    mine = next((o for o in client.orders() if o["order_id"] == str(oid)), None)
                    if mine is None:
                        rec(BAD, "order appears in the order book", "not found")
                    elif mine["status"] == "COMPLETE":
                        rec(WARN, "order FILLED", f"you now hold {mine['filled_quantity']} of {inst['tradingsymbol']} @ {mine['average_price']} — square it off in your Angel app")
                        return res
                    else:
                        rec(OK, "order appears in the order book", f"status {mine['status']}")
                        client.modify_order(variety="regular", order_id=oid, price=0.10)
                        time.sleep(1.0)
                        mod = next((o for o in client.orders() if o["order_id"] == str(oid)), None)
                        rec(OK if mod and abs(mod["price"] - 0.10) < 0.001 else BAD, "modify price", f"book now shows ₹{mod['price'] if mod else '?'}")
                        client.cancel_order(variety="regular", order_id=oid)
                        time.sleep(1.0)
                        fin = next((o for o in client.orders() if o["order_id"] == str(oid)), None)
                        rec(OK if fin and fin["status"] == "CANCELLED" else BAD, "cancel", f"status {fin['status'] if fin else '?'}")
                except Exception as e:
                    rec(BAD, "order lifecycle", str(e))
    return res


def conclusive(res) -> bool:
    """A pass that may unlock live trading: no failure, the market was open, and option OI was actually seen."""
    if any(s == BAD for s, _, _ in res):
        return False
    if any(l == "market is closed" for _, l, _ in res):
        return False
    return any(s == OK and l == "option open interest" for s, l, _ in res)


def summarize(res, say=print) -> int:
    bad = [r for r in res if r[0] == BAD]
    warn = [r for r in res if r[0] == WARN]
    say("\n" + "─" * 60)
    if bad:
        say(f"{BAD} {len(bad)} check(s) FAILED — keep Angel in paper mode and send this output to the owner:")
        for s, l, d in bad:
            say(f"     • {l}: {d}")
        return 1
    say(f"{OK} no failures" + (f", {len(warn)} warning(s) to read" if warn else ""))
    say("   Real orders are still OFF until the owner sets `paper = false` in the profile." )
    return 0


def record_if_conclusive(res, client_code, say=print) -> bool:
    """Unlock live trading for this account only when the run proves everything the bot relies on."""
    if not conclusive(res):
        say(f"{WARN} This run does NOT unlock live trading (it needs a clean run during market hours, 09:15-15:30 IST, with option open interest present).")
        return False
    import broker
    broker.write_verified(client_code)
    say(f"{OK} Verification recorded for this account — live trading is now allowed for 30 days (still needs `paper = false`).")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test-order", action="store_true", help="also run the one-order lifecycle test (asks you to type YES)")
    a = ap.parse_args(argv)
    import paths
    paths.load_instance_env()
    need = ["ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET"]
    miss = [k for k in need if not os.environ.get(k, "").strip()]
    if miss:
        print(f"Missing in the .env: {', '.join(miss)}", file=sys.stderr)
        return 2
    from auth.angel_login import login
    from broker.angel import AngelClient
    print("Logging in to Angel…")
    try:
        jwt = login(os.environ["ANGEL_API_KEY"], os.environ["ANGEL_CLIENT_CODE"], os.environ["ANGEL_PASSWORD"], os.environ["ANGEL_TOTP_SECRET"])
    except Exception as e:
        print(f"{BAD} login failed: {e}")
        return 1
    print(f"{OK} logged in\n")
    client = AngelClient(os.environ["ANGEL_API_KEY"], jwt, paper=False)      # LIVE client, used read-only (plus --test-order)
    res = run(client, test_order=a.test_order)
    rc = summarize(res)
    if rc == 0:
        record_if_conclusive(res, os.environ["ANGEL_CLIENT_CODE"])
    return rc


if __name__ == "__main__":
    sys.exit(main())
