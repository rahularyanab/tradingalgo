import os
from dotenv import load_dotenv

load_dotenv()

# ── Zerodha credentials ───────────────────────────────────────────
KITE_API_KEY    = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")

# ── Telegram credentials ──────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Instrument ────────────────────────────────────────────────────
NIFTY_SYMBOL       = "NIFTY 50"
NIFTY_EXCHANGE     = "NSE"
NIFTY_INSTRUMENT_TOKEN = 256265   # Zerodha token for NIFTY 50 index
NFO_EXCHANGE       = "NFO"
UNDERLYING         = "NIFTY"

# ── Candle settings ───────────────────────────────────────────────
TIMEFRAME          = "15minute"
CANDLE_COUNT       = 100          # candles to fetch for analysis

# ── Strategy parameters ───────────────────────────────────────────
RSI_PERIOD         = 14
PIVOT_LOOKBACK      = 2     # bars each side to confirm a pivot (30 min on 15-min chart)
CLUSTER_BAND        = 75    # price points — pivots within this range form one S/R cluster
CLUSTER_MIN_TOUCHES = 2     # minimum pivot touches to count as a valid S/R level
SR_PROXIMITY_PCT    = 0.005 # within 0.5% of cluster center = "at the level" (~120 pts)
PROXIMITY_PCT       = 0.003 # kept for RSI divergence rolling-window check

# ── RSI divergence (rolling window — no pivot lag) ────────────────
DIVERGENCE_LOOKBACK     = 8    # bars to look back (~2 hrs on 15-min chart)
DIVERGENCE_RSI_MIN_DIFF = 1.0  # RSI must be this many points weaker/stronger to count

# ── COI-based call/put writing signal ────────────────────────────
COI_NEARBY_STRIKES  = 3    # ATM + this many strikes above spot for call-write check
COI_NEARBY_FRACTION = 0.15 # fresh COI at nearby strikes / total COI ≥ this → signal

# ── Option settings ───────────────────────────────────────────────
NIFTY_LOT_SIZE     = 65
STRIKE_STEP        = 50           # Nifty strikes in multiples of 50
OTM_STRIKES        = 1            # how many strikes OTM to go when selling
MAX_OTM_STRIKES    = 6            # never sell more than 6 strikes (300 pts) from spot

# ── Hedge leg (far OTM buy for margin reduction) ──────────────────
HEDGE_PRICE_MIN    = 3.0          # minimum LTP for hedge option
HEDGE_PRICE_MAX    = 8.0          # maximum LTP for hedge option
HEDGE_PRICE_TARGET = 5.5          # ideal LTP ~₹5-6

# ── Risk / SL settings ───────────────────────────────────────────
SL_BUFFER_POINTS   = 50           # spot points beyond trendline = SL
TARGET_DECAY_PCT   = 0.65         # target = 65% premium decay

# ── Signal thresholds ─────────────────────────────────────────────
PCR_BULLISH        = 1.2          # PCR above this → bullish sentiment
PCR_BEARISH        = 0.8          # PCR below this → bearish sentiment
PCR_NEUTRAL_LOW    = 0.8          # PCR range for non-directional / strangle
PCR_NEUTRAL_HIGH   = 1.2
RSI_NEUTRAL_LOW    = 40           # RSI range for non-directional / strangle
RSI_NEUTRAL_HIGH   = 60
MIN_SIGNAL_SCORE   = 2            # minimum out of 3 to send alert

# ── Intraday settings ─────────────────────────────────────────────
INTRADAY_MODE      = True
ENTRY_START_HOUR   = 10
ENTRY_START_MIN    = 0            # first entry only after 10:00 (avoids opening noise)
FORCE_EXIT_HOUR    = 14
FORCE_EXIT_MIN     = 55           # entry window closes at 14:55 — no new trades this late (unchanged)
EOD_FLATTEN_HOUR   = 15           # 3:15 PM — single full flatten of whatever's open (directionals every
EOD_FLATTEN_MIN    = 15           # day; strangles only the day before their own expiry). Replaces the old
                                   # 2:55/3:25 split — the 3:25 leg sat 5 min from close, in the manipulation window.
STRANGLE_CUTOFF_HOUR   = 14       # strangles allowed until 14:45 (overnight hold)
STRANGLE_CUTOFF_MIN    = 45
FRIDAY_STRANGLE_CUTOFF = 12       # no new strangles on Friday after 12:00 PM (gap risk)
STRANGLE_SL_BUFFER     = 50       # same as directional (80 pts made losses bigger)

# ── Lot sizing ────────────────────────────────────────────────────
# These are STARTING sizes only. Positions can pyramid up to MAX_LOTS_*
# below while in profit — see the Pyramiding section.
LOTS_STRONG        = 3            # directional STRONG signal (3/3)
LOTS_MODERATE      = 2            # directional MODERATE signal (2/3)
LOTS_STRANGLE      = 2            # each leg of a strangle (non-directional)

# ── Pyramiding (scale into strength, never into weakness) ─────────
# Never applied on entry — only added on top of an already-profitable
# position, so the risk on fresh capital never changes from today's sizing.
MAX_LOTS_DIRECTIONAL    = 6       # pyramid ceiling for CALL_SELL/PUT_SELL — same cap for STRONG and MODERATE starts
MAX_LOTS_STRANGLE_LEG   = 4       # pyramid ceiling per leg for STRANGLE
PYRAMID_CONFIRM_CANDLES = 2       # consecutive in-profit + same-direction MODERATE+ candles before adding lots
PYRAMID_STEP_LOTS       = 2       # lots added per pyramid step (last step trimmed to not overshoot the cap)

# ── Scheduler ─────────────────────────────────────────────────────
MARKET_OPEN_HOUR   = 9
MARKET_OPEN_MIN    = 15
MARKET_CLOSE_HOUR  = 15
MARKET_CLOSE_MIN   = 30

# ── Strike rolling ────────────────────────────────────────────────
ROLL_THRESHOLD_PTS       = 100   # OI put/call wall shift (points) to trigger roll
ROLL_CUTOFF_HOUR         = 13
ROLL_CUTOFF_MIN          = 30    # no rolls after 13:30
MAX_ROLLS_PER_DAY        = 2
BREAKOUT_CONFIRM_CANDLES = 2     # consecutive scans above resistance to confirm breakout
REVERSAL_CONFIRM_CANDLES = 2     # consecutive reversal candles before adding hedge
CLEAN_CONFIRM_CANDLES    = 2     # consecutive clean candles before removing hedge
DAILY_MAX_LOSS           = 5000  # ₹ daily hard stop (realised + unrealised) — force-closes the open
                                  # position the instant it's breached, in addition to blocking new entries
PROFIT_LOCK_ARM_PNL      = 1500  # ₹ unrealised (at entry lot size) to arm the trailing lock
PROFIT_LOCK_GIVEBACK_PCT = 0.35  # exit if unrealised retraces this fraction from its peak once armed

# ── Profit-booking ladder ──────────────────────────────────────────
# Replaces the old single-shot "cut to 1 lot at ₹5000" partial lock with
# staged booking; the trailing lock above still protects whatever's left
# running between/after these steps.
PROFIT_BOOK_STEP1_PNL = 5000     # ₹ unrealised → book PROFIT_BOOK_STEP_LOTS lots
PROFIT_BOOK_STEP2_PNL = 7500     # ₹ unrealised → book another PROFIT_BOOK_STEP_LOTS lots (remainder runs to EOD)
PROFIT_BOOK_STEP_LOTS = 2        # lots booked at each ladder step

# ── Same-direction re-entry after a SIGNAL_EXIT ───────────────────
# A signal exit blocks re-entry in that direction briefly, then allows it back
# ONLY on a fresh STRONG (3/3) signal — treats the exit as a possible shakeout
# rather than a permanent lockout, but caps how often it can happen per day.
REENTRY_COOLDOWN_CANDLES = 2     # candles to wait after a SIGNAL_EXIT before a same-direction re-entry
MAX_SAME_DIR_REENTRIES   = 2     # max STRONG re-entries per direction per day

# ── Low-premium expiry fallback ────────────────────────────────────
# In a low-VIX regime, near-week OTM premium can be too thin to be worth the
# gamma risk. If the chosen strike's premium is below this, try next week's
# expiry first (more time value at the same OTM distance = higher premium,
# LOWER gamma, without moving the strike closer to spot). If next week's own
# option-derived signals don't independently confirm the same trade, fall
# back to this week's cheap signal but with pyramiding disabled for that
# trade — keeps something on screen without letting a thin-premium position
# scale up into an Aug-20-style loss.
LOW_PREMIUM_THRESHOLD = 50       # ₹ — below this, try next week's expiry first

# ── Whole-account MTM guard (every open position, algo's own trade included) ──
TOTAL_MTM_MAX_LOSS = 20000  # ₹ combined loss across ALL open positions → square off everything
# Pause dates are no longer stored here — see execution/mtm_guard.py (JSON-backed,
# controllable via the Telegram /pausemtm and /resumemtm commands).
