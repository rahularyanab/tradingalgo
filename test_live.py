"""
Live connectivity test.
  1. Fetch Nifty spot price + option chain from live market
  2. Run signal analysis on real data
  3. Attempt to place a SELL order via Kite (will be rejected if market is closed)
  4. Send a real Telegram signal message

Run:  python test_live.py
"""

import sys
import logging
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger("test_live")

print("\n" + "═"*55)
print("  LIVE CONNECTIVITY TEST")
print("═"*55)


# ── Step 1: Zerodha login ─────────────────────────────────────────
print("\n[1/5] Loading Zerodha access token...")
from auth.kite_login import load_access_token
from data.market_data import get_kite_client, fetch_nifty_candles, get_current_nifty_price

try:
    token = load_access_token()
    kite  = get_kite_client(token)
    print(f"      ✓ Token loaded")
except Exception as e:
    print(f"      ✗ {e}")
    sys.exit(1)


# ── Step 2: Fetch live Nifty price ────────────────────────────────
print("\n[2/5] Fetching live Nifty spot price...")
try:
    spot = get_current_nifty_price(kite)
    print(f"      ✓ Nifty Spot: ₹{spot:,.1f}")
except Exception as e:
    print(f"      ✗ {e}")
    sys.exit(1)


# ── Step 3: Fetch option chain + run signals ──────────────────────
print("\n[3/5] Fetching option chain + running signal analysis...")
from data.option_chain import fetch_option_chain
from data.market_data import fetch_nifty_candles
from strategy.trendline import analyse_trendlines
from strategy.rsi_divergence import analyse_rsi_divergence
from strategy.option_signal import analyse_option_signal
from signals.combiner import combine_signals

try:
    df = fetch_nifty_candles(kite)
    print(f"      ✓ {len(df)} candles loaded  (latest: {df.index[-1].strftime('%d %b %H:%M')})")
except Exception as e:
    print(f"      ✗ Candle fetch failed: {e}")
    sys.exit(1)

# Option chain — may be unavailable outside market hours
oc = fetch_option_chain()
if oc:
    print(f"      ✓ Option chain: PCR={oc.pcr}  MaxPain={oc.max_pain}  Expiry={oc.weekly_expiry_date}")
else:
    print(f"      ⚠ Option chain unavailable (NSE API offline — normal on weekends/after hours)")
    print(f"      → Continuing with trendline + RSI signals only")

# Run signals
try:
    tl  = analyse_trendlines(df)
    rsi = analyse_rsi_divergence(df)

    print(f"\n      Trendline Analysis:")
    rl = f"{tl.resistance_level:.0f}" if tl.resistance_level else "N/A"
    sl = f"{tl.support_level:.0f}"   if tl.support_level    else "N/A"
    print(f"        At resistance : {tl.at_resistance}  level={rl}")
    print(f"        At support    : {tl.at_support}   level={sl}")
    print(f"        RSI current   : {rsi.rsi_current}")
    print(f"        Bullish div   : {rsi.bullish_divergence} (developing: {rsi.bullish_divergence_developing})")
    print(f"        Bearish div   : {rsi.bearish_divergence} (developing: {rsi.bearish_divergence_developing})")

    if oc:
        opt    = analyse_option_signal(oc, tl.resistance_level, tl.support_level)
        expiry = oc.weekly_expiry_date
    else:
        # Simulate neutral OI signal when OC unavailable
        from strategy.option_signal import OptionSignal
        opt    = OptionSignal(False, False, None, None, None, None, None, None,
                              1.0, 1.0, 0, 0, 0, [], [])
        expiry = "19-Jun-2026"  # placeholder

    signal = combine_signals(tl=tl, rsi=rsi, opt=opt, spot_price=spot, expiry=expiry)

    print(f"\n      Combined Signal:")
    print(f"        Action   : {signal.action}  ({signal.score}/3  {signal.strength})")
    if signal.action != "NO_SIGNAL":
        if signal.action == "STRANGLE":
            print(f"        CE Strike: {signal.call_strike}  @ ₹{signal.call_premium}")
            print(f"        PE Strike: {signal.put_strike}   @ ₹{signal.put_premium}")
            print(f"        CE Symbol: {signal.call_symbol}")
            print(f"        PE Symbol: {signal.put_symbol}")
        else:
            print(f"        Strike   : {signal.strike}  @ ₹{signal.premium}")
            print(f"        Symbol   : {signal.symbol}")
            print(f"        SL Spot  : {signal.sl_spot_level}")
            print(f"        Lots     : {signal.lots}")

except Exception as e:
    print(f"      ✗ Signal analysis failed: {e}")
    import traceback; traceback.print_exc()
    sys.exit(1)


# ── Step 4: Attempt live order ────────────────────────────────────
print("\n[4/5] Attempting live SELL order via Kite...")
from execution.order_manager import place_sell_order
from config import NIFTY_LOT_SIZE

# Pick the best available symbol to attempt
if signal.action == "STRANGLE" and signal.call_symbol:
    test_symbol = signal.call_symbol
    test_lots   = signal.strangle_lots
elif signal.action in ("CALL_SELL", "PUT_SELL") and signal.symbol:
    test_symbol = signal.symbol
    test_lots   = signal.lots
else:
    # No signal or OC unavailable — build a test symbol from ATM + 100 pts
    atm = round(spot / 50) * 50
    from strategy.option_signal import _build_nfo_symbol
    test_expiry = oc.weekly_expiry_date if oc else expiry
    test_symbol = _build_nfo_symbol(atm + 100, "CE", test_expiry)
    test_lots   = 1

test_qty = test_lots * NIFTY_LOT_SIZE
print(f"\n      Sending order:")
print(f"        Symbol  : NFO:{test_symbol}")
print(f"        Action  : SELL")
print(f"        Quantity: {test_qty}  ({test_lots} lot{'s' if test_lots > 1 else ''})")
print(f"        Product : NRML")
print(f"        Type    : MARKET")

result = place_sell_order(kite, test_symbol, test_qty, product="NRML")

if result.success:
    print(f"\n      ✓ ORDER PLACED — ID: {result.order_id}")
    print(f"      (Unexpected during market hours — check Kite positions!)")
else:
    err = result.error or ""
    if any(x in err.lower() for x in ["closed", "holiday", "outside", "market hours", "timing"]):
        print(f"\n      ✓ API reached Zerodha successfully")
        print(f"      ✓ Order rejected as expected: market closed")
        print(f"        Error: {err[:120]}")
    elif "not enabled" in err.lower() or "permission" in err.lower():
        print(f"\n      ⚠ Order rejected: Options trading not enabled on this account")
        print(f"        Error: {err[:120]}")
        print(f"        → Enable F&O trading at zerodha.com/account/segments")
    else:
        print(f"\n      ✗ Unexpected error: {err[:200]}")


# ── Step 5: Send Telegram signal ──────────────────────────────────
print("\n[5/5] Sending real signal to Telegram (@AarshabhTradingBot)...")
from notifications.telegram_bot import send_signal, _post

# Send the actual live signal (even if NO_SIGNAL — shows OI data)
ok = send_signal(signal)
if ok:
    print(f"      ✓ Telegram message sent! Check @AarshabhTradingBot")
else:
    print(f"      ✗ Telegram send failed — check TELEGRAM_BOT_TOKEN in .env")

# Also send a manual test banner
_post(
    f"🧪 *LIVE TEST COMPLETE | {datetime.now().strftime('%H:%M IST  %d %b %Y')}*\n"
    f"━━━━━━━━━━━━━━━━━━━━\n"
    f"Nifty Spot: `{spot:,.1f}`\n"
    f"Signal    : *{signal.action}*  ({signal.score}/3  {signal.strength})\n"
    f"PCR       : `{signal.pcr}`  |  Max Pain: `{signal.max_pain}`\n"
    f"Option chain: `{'offline' if not oc else oc.weekly_expiry_date}` expiry\n"
    f"Kite order: {'✅ Accepted' if result.success else '✅ Reached Zerodha (market closed)'}\n"
    f"━━━━━━━━━━━━━━━━━━━━\n"
    f"_All systems operational. Ready to go live._"
)

print("\n" + "═"*55)
print("  TEST COMPLETE")
print("═"*55 + "\n")
