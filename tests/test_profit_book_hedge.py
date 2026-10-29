"""
Offline tests: a profit-booking partial exit must unwind the MATCHING slice of the hedge.

Run from the repo root:   python3 tests/test_profit_book_hedge.py
Everything external is faked (Kite, Telegram, journal CSV, state file) — no real orders are
possible from here.
"""
import csv, os, re, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, os.getcwd())
os.environ["TELEGRAM_BOT_TOKEN"] = os.environ["TELEGRAM_CHAT_ID"] = ""     # tests must never post to the live Telegram bot (the local .env holds its token)
import main

TMP = Path(tempfile.mkdtemp())
main.JOURNAL_PATH = TMP / "journal.csv"
main._STATE_FILE = TMP / "state.json"
POSTS, ERRS, ORDERS = [], [], []

main._post = lambda text: POSTS.append(text) or True
main.send_error_alert = lambda text: ERRS.append(text)
main.send_trade_journal_entry = lambda *a, **k: True
main._is_market_open = lambda: True
main._get_kite = lambda: object()          # order functions are faked below, so the client is never used
_ok = lambda: NS(success=True, error=None)
main.place_buy_order = lambda kite, sym, qty, product="NRML", price=None, exchange=None: (ORDERS.append(("BUY", sym, qty)) or _ok())
PRICES = []


def _fake_sell(kite, sym, qty, product="NRML", price=None, exchange=None):
    ORDERS.append(("SELL", sym, qty)); PRICES.append(price)
    return _ok()


main.place_sell_order = _fake_sell
REAL_BUY, REAL_SELL = main.place_buy_order, main.place_sell_order

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   {extra}"))


def md_ok(text):
    """Legacy Telegram Markdown only parses if _ * ` come in pairs (outside code spans)."""
    if text.count("`") % 2:
        return False
    plain = re.sub(r"`[^`]*`", "", text)
    return plain.count("_") % 2 == 0 and plain.count("*") % 2 == 0


def reset():
    POSTS.clear(); ERRS.clear(); ORDERS.clear(); PRICES.clear()
    main._get_kite = lambda: object()
    main.monitor.clear_trade(); main.strangle_legs = None
    main._day_realised_pnl = 0.0; main._day_profit_locked = False
    main.place_buy_order, main.place_sell_order = REAL_BUY, REAL_SELL
    for p in (main.JOURNAL_PATH, main._STATE_FILE):
        if p.exists():
            p.unlink()


def journal():
    return list(csv.DictReader(open(main.JOURNAL_PATH))) if main.JOURNAL_PATH.exists() else []


def oc(call=None, put=None):
    mk = lambda d: {k: NS(ltp=v) for k, v in (d or {}).items()}
    return NS(call_data=mk(call), put_data=mk(put), weekly_expiry_date="19-Oct-2026")


def decision(lots=2):
    return main.ManagementDecision(action=main.PROFIT_BOOK, lots=lots, reason="Unrealised hit step-1", score=0)


def call_trade(lots=195, hedge=True):
    t = main.TradeState(
        action="CALL_SELL", strike=22600, symbol="NIFTY26O1922600CE", entry_time=datetime.now() - timedelta(hours=1),
        entry_premium=149.37, entry_spot=22550.0, sl_spot_level=22616.2, expiry="19-Oct-2026", lots=lots,
        hedge_symbol="NIFTY26O1923200CE" if hedge else None, hedge_strike=23200 if hedge else None,
        hedge_entry_premium=6.15 if hedge else None)
    main.monitor.set_trade(t)
    return t


HSYM = "NIFTY26O1923200CE"
OC = lambda: oc(call={22600: 100.0, 23200: 4.0})

print("\n[1] your scenario: 3 lots of short + 3 lots of hedge, book 2")
reset()
t = call_trade()
main._handle_management_decision(decision(2), t, 22580.0, None, None, None, OC(), None)
check("BUY 130 short first, then SELL 130 hedge", ORDERS == [("BUY", "NIFTY26O1922600CE", 130), ("SELL", HSYM, 130)], str(ORDERS))
check("1 lot (65) still tracked", t.lots == 65, str(t.lots))
r = journal()[-1] if journal() else {}
check("journal row has the hedge exit price and hedge P&L (was blank / 0)",
      r.get("exit_reason") == "PROFIT_BOOK" and r.get("hedge_exit_premium") not in ("", None)
      and abs(float(r["hedge_pnl"]) - (4.0 - 6.15) * 130) < 2, str(r))
check("net = short + hedge", abs(float(r["net_pnl"]) - (float(r["main_pnl"]) + float(r["hedge_pnl"]))) < 2, str(r))
check("day realised P&L includes the hedge slice", abs(main._day_realised_pnl - float(r["net_pnl"])) < 2, str(main._day_realised_pnl))
tg = [p for p in POSTS if "PROFIT BOOKED" in p]
check("Telegram message shows the hedge and is valid markdown", len(tg) == 1 and "Hedge" in tg[0] and md_ok(tg[0]), str(tg))

print("\n[2] the later full exit now closes the REST of the hedge")
ORDERS.clear()
main._close_directional_slice(t, object(), 22560.0, OC(), t.lots, "EOD_FLATTEN", "x")
final = sum(o[2] for o in ORDERS if o[0] == "SELL" and o[1] == HSYM)
check("final exit sells the remaining 65 of hedge", final == 65, str(ORDERS))
check("hedge sold over both steps == the 195 bought at entry (nothing stranded)", 130 + final == 195)

print("\n[3] a rejected buy-back must not touch the hedge")
reset()
t = call_trade()
main.place_buy_order = lambda kite, sym, qty, product="NRML", price=None, exchange=None: (ORDERS.append(("BUY", sym, qty)) or NS(success=False, error="rejected"))
main._handle_management_decision(decision(2), t, 22580.0, None, None, None, OC(), None)
check("no hedge SELL, lots unchanged, nothing journaled", not [o for o in ORDERS if o[0] == "SELL"] and t.lots == 195 and not journal(), str(ORDERS))

print("\n[4] a rejected hedge sell → loud alert; the short side is still booked and journaled")
reset()
t = call_trade()
main.place_sell_order = lambda kite, sym, qty, product="NRML", price=None, exchange=None: (ORDERS.append(("SELL", sym, qty)) or NS(success=False, error="margin"))
main._handle_management_decision(decision(2), t, 22580.0, None, None, None, OC(), None)
check("alert says the HEDGE was NOT sold, valid markdown",
      any("HEDGE" in e and "NOT sold" in e for e in ERRS) and all(md_ok("⚠️ *Bot Error*\n`" + e + "`") for e in ERRS), str(ERRS))
check("lots 195→65 and hedge P&L recorded as 0", t.lots == 65 and journal() and float(journal()[-1]["hedge_pnl"]) == 0, str(journal()))

print("\n[5] a trade with no hedge behaves exactly as before")
reset()
t = call_trade(hedge=False)
main._handle_management_decision(decision(2), t, 22580.0, None, None, None, OC(), None)
check("only the short is bought back", ORDERS == [("BUY", "NIFTY26O1922600CE", 130)] and t.lots == 65, str(ORDERS))

print("\n[6] PUT_SELL")
reset()
pt = main.TradeState(action="PUT_SELL", strike=22400, symbol="NIFTY26O1922400PE", entry_time=datetime.now() - timedelta(hours=1),
                     entry_premium=100.0, entry_spot=22550.0, sl_spot_level=22434.0, expiry="19-Oct-2026", lots=195,
                     hedge_symbol="NIFTY26O1921700PE", hedge_strike=21700, hedge_entry_premium=6.0)
main.monitor.set_trade(pt)
main._handle_management_decision(decision(2), pt, 22600.0, None, None, None, oc(put={22400: 60.0, 21700: 4.5}), None)
check("BUY 130 PE short, then SELL 130 PE hedge", ORDERS == [("BUY", "NIFTY26O1922400PE", 130), ("SELL", "NIFTY26O1921700PE", 130)], str(ORDERS))

print("\n[7] STRANGLE: both legs' hedges")
reset()
st = main.TradeState(action="STRANGLE", strike=22600, symbol="NIFTY26O1922600CE + NIFTY26O1922300PE",
                     entry_time=datetime.now() - timedelta(hours=1), entry_premium=190.0, entry_spot=22450.0,
                     sl_spot_level=22650.0, sl_put=22250.0, expiry="19-Oct-2026", lots=195)
LEGS = lambda: main.StrangleLegState(
    ce_strike=22600, ce_symbol="NIFTY26O1922600CE", ce_entry_premium=100.0, pe_strike=22300, pe_symbol="NIFTY26O1922300PE", pe_entry_premium=90.0,
    hedge_ce_strike=23200, hedge_ce_symbol="NIFTY26O1923200CE", hedge_ce_entry_premium=6.0,
    hedge_pe_strike=21700, hedge_pe_symbol="NIFTY26O1921700PE", hedge_pe_entry_premium=6.0)
OCS = lambda: oc(call={22600: 70.0, 23200: 4.0}, put={22300: 60.0, 21700: 4.5})
main.monitor.set_trade(st); main.strangle_legs = LEGS()
main._handle_management_decision(decision(2), st, 22450.0, None, None, None, OCS(), None)
check("both shorts bought back first, then BOTH hedges sold, 130 each",
      [o[0] for o in ORDERS] == ["BUY", "BUY", "SELL", "SELL"]
      and {o[1] for o in ORDERS if o[0] == "SELL"} == {"NIFTY26O1923200CE", "NIFTY26O1921700PE"}
      and all(o[2] == 130 for o in ORDERS), str(ORDERS))
check("65 shares still running; journal carries the hedge P&L", st.lots == 65 and journal() and float(journal()[-1]["hedge_pnl"]) != 0, str(journal()))

print("\n[8] strangle with one buy-back rejected → no hedge orders at all")
reset()
st.lots = 195
main.monitor.set_trade(st); main.strangle_legs = LEGS()
main.place_buy_order = lambda kite, sym, qty, product="NRML", price=None, exchange=None: (
    ORDERS.append(("BUY", sym, qty)) or NS(success=(sym != "NIFTY26O1922300PE"), error="rejected"))
main._handle_management_decision(decision(2), st, 22450.0, None, None, None, OCS(), None)
check("hedges untouched, lots unchanged", not [o for o in ORDERS if o[0] == "SELL"] and st.lots == 195, str(ORDERS))


class QuoteKite:
    """Fake broker client that can price an option the chain doesn't have."""
    def __init__(self, quotes=None, fail=False): self.quotes, self.fail = quotes or {}, fail
    def ltp(self, keys):
        if self.fail: raise RuntimeError("quote unavailable")
        return {k: {"last_price": self.quotes[k]} for k in keys}


def exit_decision(reason="Bearish 2/3 for 2 consecutive candles — exiting CALL SELL to protect P&L"):
    return main.ManagementDecision(action=main.EXIT_FULL, reason=reason, reasons=[reason], score=0)


print("\n[9] signal exit (the path that failed on Oct 27): hedge sold at the same price as before")
reset()
t = call_trade()
main._handle_management_decision(exit_decision(), t, 22580.0, None, None, None, OC(), None)
check("BUY 195 short, then SELL 195 hedge", ORDERS == [("BUY", "NIFTY26O1922600CE", 195), ("SELL", HSYM, 195)], str(ORDERS))
check("hedge price unchanged from before (LTP 4.0 − 1 = 3.0)", PRICES and abs(PRICES[-1] - 3.0) < 1e-6, str(PRICES))
check("trade closed", main.monitor.trade is None)

print("\n[10] hedge strike missing from the option chain → priced from the live quote")
reset()
t = call_trade()
main._get_kite = lambda: QuoteKite({f"NFO:{HSYM}": 4.2})
chain_without_hedge = oc(call={22600: 100.0})                    # 23200 not in the chain, exactly like Oct 27
main._handle_management_decision(exit_decision(), t, 22580.0, None, None, None, chain_without_hedge, None)
check("hedge IS sold (was silently rejected as a market order)", ("SELL", HSYM, 195) in ORDERS, str(ORDERS))
check("with a real limit price from the quote (≈3.20), never None", PRICES and PRICES[-1] is not None and abs(PRICES[-1] - 3.2) < 0.06, str(PRICES))
check("no error alert on the happy fallback", not ERRS, str(ERRS))

print("\n[11] no chain price AND no quote → no doomed market order, loud alert")
reset()
t = call_trade()
main._get_kite = lambda: QuoteKite(fail=True)
main._handle_management_decision(exit_decision(), t, 22580.0, None, None, None, chain_without_hedge, None)
check("no hedge order sent", not [o for o in ORDERS if o[0] == "SELL"], str(ORDERS))
check("alert names the hedge, says NOT sold + square off, valid markdown",
      any(HSYM in e and "NOT sold" in e and "manually" in e for e in ERRS) and all(md_ok("⚠️ *Bot Error*\n`" + e + "`") for e in ERRS), str(ERRS))
check("the short was still bought back and the trade closed", ORDERS and ORDERS[0][0] == "BUY" and main.monitor.trade is None, str(ORDERS))

print("\n[12] stop-loss exit also unwinds the hedge with the same checks")
reset()
t = call_trade()
closed = main._check_sl_hit(object(), t, 22650.0, oc=OC())
check("SL fired: BUY 195 short then SELL 195 hedge @3.0", closed and ORDERS == [("BUY", "NIFTY26O1922600CE", 195), ("SELL", HSYM, 195)]
      and abs(PRICES[-1] - 3.0) < 1e-6, str((closed, ORDERS, PRICES)))
reset()
t = call_trade()
main.place_sell_order = lambda kite, sym, qty, product="NRML", price=None, exchange=None: NS(success=False, error="rejected")
main._check_sl_hit(object(), t, 22650.0, oc=OC())
check("SL exit with a rejected hedge sell → alert (was silent)", any("HEDGE" in e and "NOT sold" in e for e in ERRS), str(ERRS))
main.place_sell_order = REAL_SELL

print("\n[13] EOD flatten slice")
reset()
t = call_trade()
main._close_directional_slice(t, object(), 22560.0, chain_without_hedge, t.lots, "EOD_FLATTEN", "x")
check("EOD with the hedge strike missing: hedge sell not lost (no quote available → alert, not silence)",
      any(HSYM in e and "NOT sold" in e for e in ERRS), str(ERRS))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
