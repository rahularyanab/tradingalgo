"""
Position sizing from a person's capital.  Pure functions — no I/O, no config import.

The rule (worked out from the bot's own record, see the project notes):

  daily loss limit  = 1% of capital by default (a user may ask for up to 1.5%, never more)
  loss per lot      = what ONE lot can lose in a bad stop-out, overshoot included.
                        new user      : Rs 4,500  (1.5 x the worst stop-out seen, Rs 3,000)
                        experienced   : Rs 3,000  and only 80% of the limit is spent
  risk lots         = floor(limit x fraction / loss per lot)
  margin lots       = floor(50% of capital / margin per lot)      (a hedged Nifty lot ~ Rs 80-90K)
  starting lots     = min(risk lots, margin lots, MAX_START_LOTS)   (never below 1)

Everything else (MODERATE size, strangle size, pyramid ceilings, the rupee profit
thresholds) is derived from the starting lots so the whole bot scales together; at 3
lots every derived value equals what the bot has always used.
"""
import math
from dataclasses import dataclass, field

HARD_LIMIT_PCT = 0.015            # a daily loss limit above 1.5% of capital is refused
DEFAULT_LIMIT_PCT = 0.01
MARGIN_USE_CAP = 0.50             # plan to use at most half of the capital as margin
HARD_MAX_LOTS = 6                 # never above the bot's own pyramid ceiling

PROFILES = {                      # experience -> (loss per lot, fraction of the limit that may be risked)
    "new":         (4500.0, 1.00),
    "experienced": (3000.0, 0.80),
}
BASE_LOTS = 3                     # the size the Rs thresholds below were tuned at
BASE_THRESHOLDS = {               # name -> Rs value at BASE_LOTS (scaled with lots)
    "PROFIT_LOCK_ARM_PNL": 1500,
    "PROFIT_BOOK_STEP1_PNL": 5000,
    "PROFIT_BOOK_STEP2_PNL": 7500,
    "DAY_PROFIT_ARM_PNL": 2500,
}


class ProfileError(ValueError):
    """The user's profile can't be used. The message is written for the user."""


@dataclass
class Sizing:
    capital: float
    daily_limit: float
    limit_pct: float
    loss_per_lot: float
    margin_per_lot: float
    risk_lots: int
    margin_lots: int
    suggested_lots: int
    effective_lots: int
    lots_strong: int
    lots_moderate: int
    lots_strangle: int
    max_lots_directional: int
    max_lots_strangle_leg: int
    protected_max_lots: int
    step_lots: int
    thresholds: dict
    margin_at_start: float
    min_capital_comfortable: float
    min_capital_absolute: float
    warnings: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def _round_down(x: float, step: int) -> int:
    return int(math.floor(x / step) * step)


def _round_nearest(x: float, step: int) -> int:
    return int(round(x / step) * step)


def moderate_lots(strong_lots: int) -> int:
    """MODERATE (2/3) signals trade about two-thirds of a STRONG one — 3 -> 2, as always."""
    return max(1, int(2 * strong_lots / 3 + 0.5))


def compute(
    capital: float,
    max_daily_loss: float | None = None,
    preferred_lots: int | None = None,
    experience: str = "new",
    allow_above_suggestion: bool = False,
    margin_per_lot: float = 90_000.0,
    loss_per_lot: float | None = None,
    max_start_lots: int = 3,
    allow_pyramiding: bool = True,
) -> Sizing:
    if experience not in PROFILES:
        raise ProfileError(f'risk.experience must be "new" or "experienced" (got {experience!r}).')
    try:
        capital = float(capital)
    except (TypeError, ValueError):
        raise ProfileError("capital.total must be a number, e.g. 1000000.")
    if capital <= 0:
        raise ProfileError("capital.total must be more than zero.")

    base_loss, fraction = PROFILES[experience]
    lpl = float(loss_per_lot) if loss_per_lot else base_loss
    if lpl <= 0 or margin_per_lot <= 0:
        raise ProfileError("sizing.loss_per_lot and sizing.margin_per_lot must be positive.")

    lower_lpl = lpl < base_loss
    min_abs = lpl / (HARD_LIMIT_PCT * fraction)          # smallest capital where 1 lot fits under the 1.5% ceiling
    min_comfy = lpl / (DEFAULT_LIMIT_PCT * fraction)     # ... under the normal 1%
    warnings, notes = [], []

    if lower_lpl:
        warnings.append(f"You told the bot one lot loses at most ₹{lpl:,.0f} in a bad exit; our experience is ₹{base_loss:,.0f}. "
                        f"A fast market can take a single lot past your daily limit of ₹{float(max_daily_loss) if max_daily_loss else capital * DEFAULT_LIMIT_PCT:,.0f}.")

    # ── daily loss limit ──
    if max_daily_loss is None:
        limit = max(500, _round_down(capital * DEFAULT_LIMIT_PCT, 500))
        notes.append(f"Daily loss limit set to 1% of capital: ₹{limit:,}.")
    else:
        try:
            limit = float(max_daily_loss)
        except (TypeError, ValueError):
            raise ProfileError("risk.max_daily_loss must be a number, e.g. 10000.")
        if limit <= 0:
            raise ProfileError("risk.max_daily_loss must be more than zero.")
        if limit > capital * HARD_LIMIT_PCT:
            raise ProfileError(
                f"risk.max_daily_loss ₹{limit:,.0f} is {100 * limit / capital:.1f}% of your capital. "
                f"The most this bot accepts is 1.5% (₹{capital * HARD_LIMIT_PCT:,.0f}); we suggest 1% (₹{capital * 0.01:,.0f})."
            )
        if limit > capital * DEFAULT_LIMIT_PCT:
            warnings.append(
                f"Your daily loss limit ₹{limit:,.0f} is above 1% of capital (₹{capital * 0.01:,.0f}). "
                f"In a fast market the real loss can overshoot the limit by 30-50%."
            )

    # ── lots ──
    risk_lots = int(math.floor(limit * fraction / lpl))
    margin_lots = int(math.floor(capital * MARGIN_USE_CAP / margin_per_lot))
    if risk_lots < 1:
        raise ProfileError(
            f"Capital ₹{capital:,.0f} is too small: one lot can lose about ₹{lpl:,.0f} in a bad exit, which does not fit "
            f"inside a daily limit of ₹{limit:,.0f}. The smallest capital this bot accepts is about ₹{min_abs:,.0f} "
            f"(₹{min_comfy:,.0f} recommended)."
        )
    if margin_lots < 1:
        raise ProfileError(
            f"Capital ₹{capital:,.0f} is too small: one hedged lot needs about ₹{margin_per_lot:,.0f} of margin and we plan to "
            f"use at most half of your capital. Minimum about ₹{margin_per_lot / MARGIN_USE_CAP:,.0f}."
        )
    start_cap = max(1, min(int(max_start_lots), HARD_MAX_LOTS))
    suggested = max(1, min(risk_lots, margin_lots, start_cap))
    if min(risk_lots, margin_lots) > start_cap:
        notes.append(f"Your capital would allow more, but starting size is capped at {start_cap} lots — the size this strategy has been tested at.")
    if capital < min_comfy and max_daily_loss is not None:
        warnings.append(f"Below ₹{min_comfy:,.0f} of capital a 1% daily limit is tighter than one bad exit; you chose a larger limit to fit 1 lot.")

    eff = suggested
    if preferred_lots is not None:
        if not isinstance(preferred_lots, int) or isinstance(preferred_lots, bool) or preferred_lots < 1:
            raise ProfileError("risk.preferred_lots must be a whole number, 1 or more.")
        if preferred_lots <= suggested:
            eff = preferred_lots
        elif allow_above_suggestion:
            eff = min(preferred_lots, HARD_MAX_LOTS)
            warnings.append(
                f"You chose {preferred_lots} lots, above our suggestion of {suggested}. At {eff} lots one bad stop-out can "
                f"cost about ₹{eff * lpl:,.0f}, above your daily limit of ₹{limit:,.0f}."
            )
        else:
            eff = suggested
            warnings.append(
                f"You asked for {preferred_lots} lots; we are using {suggested}, the most that fits your daily loss limit "
                f"(each lot can lose about ₹{lpl:,.0f} in a bad exit). To override, set risk.allow_above_suggestion = true."
            )

    strong = eff
    moderate = moderate_lots(strong)
    if not allow_pyramiding:
        notes.append(f"Pyramiding is OFF: the bot never adds size to a winning trade, so you hold at most {strong} lot(s) (a strangle: {moderate} per side).")
    scale = strong / BASE_LOTS
    thresholds = {k: max(100, _round_nearest(v * scale, 100)) for k, v in BASE_THRESHOLDS.items()}

    return Sizing(
        capital=capital, daily_limit=float(limit), limit_pct=100 * float(limit) / capital,
        loss_per_lot=lpl, margin_per_lot=margin_per_lot,
        risk_lots=risk_lots, margin_lots=margin_lots, suggested_lots=suggested, effective_lots=eff,
        lots_strong=strong, lots_moderate=moderate, lots_strangle=moderate,
        max_lots_directional=min(HARD_MAX_LOTS, 2 * strong) if allow_pyramiding else strong,
        max_lots_strangle_leg=min(4, 2 * moderate) if allow_pyramiding else moderate,
        protected_max_lots=moderate, step_lots=moderate,
        thresholds=thresholds,
        margin_at_start=strong * margin_per_lot,
        min_capital_comfortable=min_comfy, min_capital_absolute=min_abs,
        warnings=warnings, notes=notes,
    )
