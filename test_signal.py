"""
Sends a sample signal to Telegram to verify formatting.
Run:  python test_signal.py
"""

from dotenv import load_dotenv
load_dotenv()

from data.option_chain import StrikeData
from signals.combiner import FinalSignal
from notifications.telegram_bot import send_signal, send_trade_warning, send_top_oi_snapshot
from signals.trade_monitor import TradeState, MonitorResult, WarningItem
from datetime import datetime


def _sample_strike(strike, oi, change_oi, ltp, iv=14.0):
    return StrikeData(strike=strike, oi=oi, change_oi=change_oi,
                      pct_change_oi=0, ltp=ltp, iv=iv, volume=12000)


def test_call_sell():
    signal = FinalSignal(
        action="CALL_SELL", strength="STRONG", score=3,
        strike=24500, symbol="NIFTY2662624500CE",
        expiry="19-Jun-2026", spot_price=24312.5,
        sl_spot_level=24530.0, target_decay=0.65,
        premium=78.5, trendline_level=24480.0,
        rsi_current=62.3, rsi_prev=68.7,
        price_prev_pivot=24210.0,
        pcr=0.71, change_pcr=0.68,
        call_wall=24500, put_wall=24000, max_pain=24200,
        top_2_calls=[
            _sample_strike(24500, 1_250_000, 85_000, 78.5),
            _sample_strike(24600, 980_000,  42_000, 45.2),
        ],
        top_2_puts=[
            _sample_strike(24000, 1_100_000, 65_000, 52.3),
            _sample_strike(23900, 870_000,   31_000, 34.7),
        ],
        reasons=[
            "Trendline resistance at 24480.0",
            "Bearish RSI divergence  (RSI 68.7→62.3)",
            "Call wall OI at 24500  |  PCR 0.71 (bearish)",
        ],
    )
    ok = send_signal(signal)
    print(f"CALL SELL signal sent: {'✓' if ok else '✗'}")


def test_put_sell():
    signal = FinalSignal(
        action="PUT_SELL", strength="MODERATE", score=2,
        strike=23900, symbol="NIFTY2662623900PE",
        expiry="19-Jun-2026", spot_price=24150.0,
        sl_spot_level=23870.0, target_decay=0.65,
        premium=42.0, trendline_level=23950.0,
        rsi_current=38.4, rsi_prev=33.1,
        price_prev_pivot=23800.0,
        pcr=1.31, change_pcr=1.24,
        call_wall=24500, put_wall=23900, max_pain=24000,
        top_2_calls=[
            _sample_strike(24500, 1_250_000, 85_000, 78.5),
            _sample_strike(24600,   980_000, 42_000, 45.2),
        ],
        top_2_puts=[
            _sample_strike(23900, 1_320_000, 95_000, 42.0),
            _sample_strike(23800,   890_000, 38_000, 28.5),
        ],
        reasons=[
            "Trendline support at 23950.0",
            "Bullish RSI divergence  (RSI 33.1→38.4)",
        ],
    )
    ok = send_signal(signal)
    print(f"PUT SELL signal sent:  {'✓' if ok else '✗'}")


def test_no_signal():
    signal = FinalSignal(
        action="NO_SIGNAL", strength="WEAK", score=1,
        strike=None, symbol=None, expiry="19-Jun-2026",
        spot_price=24250.0, sl_spot_level=None,
        target_decay=0.65, premium=None,
        trendline_level=None,
        rsi_current=51.2, rsi_prev=51.2,
        price_prev_pivot=24100.0,
        pcr=0.98, change_pcr=1.02,
        call_wall=24500, put_wall=24000, max_pain=24200,
        top_2_calls=[
            _sample_strike(24500, 1_250_000, 15_000, 78.5),
            _sample_strike(24600,   980_000, -8_000, 45.2),
        ],
        top_2_puts=[
            _sample_strike(24000, 1_100_000, 22_000, 52.3),
            _sample_strike(23900,   870_000,  5_000, 34.7),
        ],
        reasons=[],
    )
    ok = send_signal(signal)
    print(f"No-signal scan sent:   {'✓' if ok else '✗'}")


def test_trade_warning():
    trade = TradeState(
        action="PUT_SELL", strike=23900,
        symbol="NIFTY2662623900PE",
        entry_time=datetime.now(),
        entry_premium=42.0, entry_spot=24150.0,
        sl_spot_level=23870.0, expiry="19-Jun-2026",
    )
    monitor = MonitorResult(
        has_warning=True,
        warnings=[
            WarningItem("CRITICAL", "RSI_DIVERGENCE",
                "Bearish RSI divergence developing (RSI 38.4→32.1) — market may fall against PUT SELL at 23900"),
            WarningItem("CAUTION", "PCR_REVERSAL",
                "PCR fell 1.31→1.12 (-0.19) — call writing increasing, bearish sentiment building"),
        ],
        current_spot=24050.0,
        current_pcr=1.12,
        current_change_pcr=0.98,
    )
    ok = send_trade_warning(trade, monitor, 24050.0)
    print(f"Trade warning sent:    {'✓' if ok else '✗'}")


if __name__ == "__main__":
    print("Sending test messages to Telegram...\n")
    test_call_sell()
    test_put_sell()
    test_no_signal()
    test_trade_warning()
    print("\nDone. Check your Telegram for 4 messages.")
