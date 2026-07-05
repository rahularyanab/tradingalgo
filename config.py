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
FORCE_EXIT_HOUR    = 15
FORCE_EXIT_MIN     = 0            # square off ALL positions at 15:00 sharp
STRANGLE_CUTOFF_HOUR   = 14       # strangles allowed until 14:45 (overnight hold)
STRANGLE_CUTOFF_MIN    = 45
FRIDAY_STRANGLE_CUTOFF = 12       # no new strangles on Friday after 12:00 PM (gap risk)
STRANGLE_SL_BUFFER     = 50       # same as directional (80 pts made losses bigger)

# ── Lot sizing ────────────────────────────────────────────────────
LOTS_STRONG        = 3            # directional STRONG signal (3/3)
LOTS_MODERATE      = 2            # directional MODERATE signal (2/3)
LOTS_STRANGLE      = 2            # each leg of a strangle (non-directional)

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
DAILY_MAX_LOSS           = 5000  # ₹ daily circuit breaker (realised + unrealised)
PARTIAL_PROFIT_LOCK_PNL  = 5000  # ₹ unrealised threshold → exit all-but-1-lot to lock profit
