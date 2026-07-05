"""
Send a sample Telegram message using today's realistic market data
to preview the new dashboard format.
"""
import sys
sys.path.insert(0, "/opt/trading-algo")

from dataclasses import dataclass
from signals.combiner import FinalSignal
from notifications.telegram_bot import send_signal, send_paper_pnl_update

# ── Realistic data from June 17 2026 ──────────────────────────────
# Last scan of the day: ~14:45 IST, Nifty ~23,980
# RSI divergence was building (price made new highs but RSI didn't)
# Call writing was active at 24000/24100 CE
# Score was 2/3 (RSI divergence + call writing) but no trendline confirmation

@dataclass
class FakeStrike:
    strike: int
    oi: float
    change_oi: float
    ltp: float

sample_no_signal = FinalSignal(
    action="CALL_SELL",
    strength="STRONG",
    score=3,
    lots=0,
    reasons=[],
    # Common market data
    spot_price=23_980.0,
    expiry="24-Jun-2026",
    rsi_current=68.4,
    rsi_prev=72.65,
    price_prev_pivot=24_065.0,
    pcr=0.92,
    change_pcr=-0.08,
    call_wall=24_000,
    put_wall=23_800,
    max_pain=23_900,
    # Signal flags — what fires with the new cluster-based S/R
    bearish_divergence=True,         # RSI peaked at 9:45 (72.65) but price still rising
    bullish_divergence=False,
    at_resistance=True,              # cluster at 24080 (hit at 10:30 + 11:00) — now within 0.5%
    at_support=False,
    call_writing_bearish=True,       # 24000/24100 CE COI building (today's observation)
    put_writing_bullish=False,
    call_bearish_pcr=False,          # PCR 0.92 was neutral, not <0.8
    put_bullish_pcr=False,
    resistance_level=24_085.0,       # cluster center: 10:30 high 24085 + 11:00 high 24089
    support_level=23_750.0,
    # OI snapshot
    top_2_calls=[
        FakeStrike(strike=24_000, oi=1_850_000, change_oi=142_500, ltp=48.5),
        FakeStrike(strike=24_100, oi=1_420_000, change_oi=98_750,  ltp=22.3),
    ],
    top_2_puts=[
        FakeStrike(strike=23_800, oi=1_620_000, change_oi=31_200,  ltp=35.1),
        FakeStrike(strike=23_900, oi=1_210_000, change_oi=-18_500, ltp=58.7),
    ],
)

# Add strike/premium fields for CALL_SELL
sample_no_signal.strike = 24_100
sample_no_signal.premium = 48.5
sample_no_signal.sl_spot_level = 24_135.0
sample_no_signal.lots = 3
sample_no_signal.reasons = [
    "Resistance cluster at 24085 (3 touches: 9:45 / 10:30 / 11:00)",
    "Bearish RSI divergence: RSI 72.65 → 68.4 while price 24065 → 23980",
    "Call writing building at 24000/24100 CE (COI ▲1.4L)",
]

print("Sending CALL_SELL signal (what should have fired today with cluster S/R)...")
ok = send_signal(sample_no_signal)
print("Sent." if ok else "FAILED.")
