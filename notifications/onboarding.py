"""
What a new user is told on Telegram once their bot connects.

* welcome_messages()  — settings, how the bot trades, what each alert means, rules + risk
* send_welcome_once() — sent on the first start; if the profile later changes, only the new
                        settings are sent. Marked done only after every message went out.
* send_broker_verified_once() — one line the first time a market scan succeeds with the broker.

Text is plain legacy-Markdown on purpose: no underscores outside code spans (an unpaired `_`
makes Telegram reject the WHOLE message — that bug once silenced every trade record).
"""
import json
import logging
from datetime import datetime

import paths

logger = logging.getLogger(__name__)


def _rs(x: float) -> str:
    return f"₹{x:,.0f}"


def params_message(st) -> str:
    z = st.sizing
    t = z.thresholds
    lines = [
        f"*Your settings*",
        *([ "🧪 *PAPER MODE* — the bot uses real market data from your broker but its orders are SIMULATED. No money is traded and nothing appears in your broker account. "
            "Results are a little optimistic (no slippage)." ] if getattr(st, "paper", False) else []),
        f"Capital: *{_rs(z.capital)}*",
        f"Daily loss limit: *{_rs(z.daily_limit)}* ({z.limit_pct:.1f}% of capital) — when today's loss reaches this, "
        f"the bot closes everything and stops entering for the day",
        f"Starting size: *{z.lots_strong} lot(s)* on a strong signal, *{z.lots_moderate}* on a moderate one",
        f"Strangle (when the market is range-bound): *{z.lots_strangle}* lot(s) each side",
        (f"Size can grow only while a trade is in profit, up to *{z.max_lots_directional}* lots" if z.max_lots_directional > z.lots_strong
         else "Size is *never added* to a trade — you hold exactly your starting lots"),
        f"Margin needed at your starting size: about {_rs(z.margin_at_start)} ({100 * z.margin_at_start / z.capital:.0f}% of capital)",
        f"Profit levels for your size: trailing lock from {_rs(t['PROFIT_LOCK_ARM_PNL'])}, profit booked at "
        f"{_rs(t['PROFIT_BOOK_STEP1_PNL'])} and {_rs(t['PROFIT_BOOK_STEP2_PNL'])}, day profit-lock arms at {_rs(t['DAY_PROFIT_ARM_PNL'])} banked",
    ]
    lim = int(st.config_values.get("ENTRY_FAIL_LIMIT", 0) or 0)
    if lim > 0:
        lines.append(f"If *{lim}* entry attempts fail in a day (for example the broker rejects an order for lack of margin), the bot stops entering for that day and tells you. /resume gives it a fresh start.")
    for n in z.notes:
        lines.append(f"ℹ️ {n}")
    for w in z.warnings:
        lines.append(f"⚠️ {w}")
    return "\n".join(lines)


def welcome_messages(st) -> list:
    z = st.sizing
    m1 = "\n".join([
        f"✅ *Connected* — hello {st.name}!",
        "This chat is now linked to your trading bot. Every entry, exit, profit booking and warning will arrive here.",
        "Entries start at 10:00 AM IST on market days; nothing happens outside market hours.",
        "",
        params_message(st),
    ])
    m2 = "\n".join([
        "*How the bot trades*",
        "• Every 15 minutes it reads Nifty: trendline support/resistance, RSI divergence and option-chain open interest.",
        "• It sells ONE Nifty option — a call when the market looks weak, a put when it looks strong — or a strangle when it is range-bound.",
        "• Every sold option has a cheap far-out hedge bought first, so the loss on a bad day is capped.",
        "• Everything is closed by 3:25 PM the same day. Nothing is held overnight.",
        "",
        "*Built-in protection*",
        "• Stop-loss is checked every minute.",
        f"• Daily loss limit: {_rs(z.daily_limit)}.",
        f"• Once about {_rs(z.thresholds['DAY_PROFIT_ARM_PNL'])} of profit is booked, the rest of the day uses smaller size and the day is not allowed to turn into a loss.",
        "• New entries after 2:00 PM use half size.",
        "",
        "*What the alerts mean*",
        "📊 SCAN — the bot looked at the market (trade or no trade)",
        "📋 TRADE RECORD — a trade closed, with the reason and profit or loss",
        "🔒 PROFIT BOOKED / PROFIT LOCK — profit was banked or protected",
        "📈 PYRAMID ADD — size added to a trade that is in profit",
        "🚨 SL HIT or AT RISK — the stop-loss was hit or is close",
        "🛑 DAILY LOSS LIMIT — the day's limit was reached, trading stops",
        "⚠️ Bot Error — something needs attention; read it and tell the person who set this up",
    ])
    m3 = "\n".join([
        "*Commands*",
        "/params — show your settings",
        "/pnl — daily profit and loss of the bot's trades (this month)",
        "/sr — the support and resistance levels the bot is tracking",
        "/pause — stop NEW trades if anything looks wrong (an open trade keeps its stop-loss and is closed by 3:25 PM)",
        "/resume — allow new trades again",
        "/status — is the bot paused or running, and is a trade open",
        "/start — show this guide again",
        "/help — list the commands",
        "",
        "*Please remember*",
        f"1. Keep enough free margin in your account — at least {_rs(z.margin_at_start)}, more if the bot adds size.",
        "2. Avoid trading the same Nifty options by hand. If you do close one of the bot's positions yourself, it notices within a minute or two and will not place orders against it.",
        "3. The daily loss limit is a target, not a guarantee. In a fast market the exit price can be worse; losses 30 to 50 percent beyond the limit have happened.",
        "4. If you see a warning about login or access, tell the person who set this up.",
        "5. This is a test programme, not investment advice. Options selling can lose money. Start small and watch it.",
        *([ "6. You are in PAPER MODE: the owner will tell you when real orders are switched on." ] if getattr(st, "paper", False) else []),
    ])
    return [m1, m2, m3]


def _read_flag() -> dict:
    try:
        return json.loads(paths.ONBOARD_FLAG.read_text())
    except Exception:
        return {}


def _write_flag(st) -> None:
    try:
        paths.ONBOARD_FLAG.write_text(json.dumps({"hash": st.profile_hash, "at": datetime.now().isoformat(timespec="seconds")}))
    except Exception as e:
        logger.warning(f"Could not record onboarding state: {e}")


def send_welcome_once(st, post=None) -> str:
    """
    Returns "sent" (first connection), "updated" (settings changed), "skipped" (already done)
    or "failed" (Telegram did not accept it — will retry on the next start).
    """
    if post is None:
        from notifications.telegram_bot import _post as post
    flag = _read_flag()
    if flag.get("hash") == st.profile_hash:
        return "skipped"
    if not flag:
        msgs, kind = welcome_messages(st), "sent"
    else:
        msgs, kind = ["⚙️ *Your settings were updated*\n\n" + params_message(st)], "updated"
    for m in msgs:
        if not post(m):
            logger.warning("Welcome message was not delivered — will retry on next start.")
            return "failed"
    _write_flag(st)
    logger.info(f"Telegram onboarding {kind} for {st.name}.")
    return kind


def send_broker_verified_once(st, post=None) -> bool:
    """One line, once ever: the first scan that got market data through the broker."""
    if post is None:
        from notifications.telegram_bot import _post as post
    if paths.BROKER_OK_FLAG.exists():
        return False
    tail = ("Orders are SIMULATED (paper mode), so nothing is traded with real money." if getattr(st, "paper", False)
            else "The bot will trade on its own from 10:00 AM when its signals are met.")
    ok = post(f"🔌 *Broker connected* — your {st.broker.title()} session works and the first market scan completed. {tail}")
    if ok:
        try:
            paths.BROKER_OK_FLAG.write_text(datetime.now().isoformat(timespec="seconds"))
        except Exception as e:
            logger.warning(f"Could not record broker verification: {e}")
    return ok
