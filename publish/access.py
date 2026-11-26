"""
Who gets the live trade alerts: a free first month for everyone, then paid time added by the owner.

Rules (Rahul, 2026-11-26):
  * Everyone gets TRIAL_DAYS free from their first Start in the bot.
  * After that, live DMs and the bot's buttons stop until the owner records a payment
    (/paid <who> <months> [amount]); they keep the public channel, which runs 15 minutes late.
  * The owner (TG_OWNER_ID) always has access.
  * Payments are recorded on the subscriber (date, months, amount) — the owner's own record.

A subscriber record (logs/tg_subscribers.json, keyed by Telegram chat id) gains:
    trial_until, paid_until (ISO datetimes), payments [..], revoked, notified {end, sent}
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Optional

TRIAL_DAYS = 30
MONTH_DAYS = 30
REMIND_DAYS = (5, 1)                 # reminders this many days before access ends
NOTIFY_FROM, NOTIFY_UNTIL = 9, 21    # IST hours during which reminders may be sent


def _dt(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


def ensure_trial(sub: dict, now: datetime) -> None:
    """Every subscriber has a trial end, counted from when they joined (or now, for old records)."""
    if not sub.get("trial_until"):
        start = _dt(sub.get("joined")) or now
        if start.tzinfo is None:
            start = start.replace(tzinfo=now.tzinfo)
        sub["trial_until"] = (start + timedelta(days=TRIAL_DAYS)).isoformat(timespec="seconds")


def access_until(sub: dict) -> Optional[datetime]:
    ends = [d for d in (_dt(sub.get("trial_until")), _dt(sub.get("paid_until"))) if d]
    return max(ends) if ends else None


def status(sub: dict, now: datetime, is_owner: bool = False) -> str:
    """owner · paid · trial · expired"""
    if is_owner:
        return "owner"
    end = access_until(sub)
    if end is None or now >= end:
        return "expired"
    paid = _dt(sub.get("paid_until"))
    return "paid" if paid and now < paid else "trial"


def has_access(sub: dict, now: datetime, is_owner: bool = False) -> bool:
    return status(sub, now, is_owner) != "expired"


def days_left(sub: dict, now: datetime) -> int:
    """Whole days of access left, rounded up (0 once expired)."""
    end = access_until(sub)
    return math.ceil((end - now).total_seconds() / 86400) if end and end > now else 0


def add_paid(sub: dict, now: datetime, months: int, amount: Optional[float], by: str) -> datetime:
    """Add paid months from the later of now and the current end. Returns the new end."""
    end = access_until(sub)
    start = end if end and end > now else now
    new_end = start + timedelta(days=MONTH_DAYS * months)
    sub["paid_until"] = new_end.isoformat(timespec="seconds")
    sub.pop("revoked", None)
    sub.setdefault("payments", []).append({"at": now.isoformat(timespec="seconds"), "months": months,
                                           "amount": amount, "by": by, "until": sub["paid_until"]})
    return new_end


def extend(sub: dict, now: datetime, days: int) -> datetime:
    end = access_until(sub)
    start = end if end and end > now else now
    new_end = start + timedelta(days=days)
    key = "paid_until" if _dt(sub.get("paid_until")) else "trial_until"
    sub[key] = new_end.isoformat(timespec="seconds")
    sub.pop("revoked", None)
    return new_end


def revoke(sub: dict, now: datetime) -> None:
    for key in ("trial_until", "paid_until"):
        if sub.get(key):
            sub[key] = now.isoformat(timespec="seconds")
    sub["revoked"] = now.isoformat(timespec="seconds")


def find(subs: dict, who: str) -> list:
    """Chat ids matching a chat id, @username or (case-insensitive) name prefix."""
    who = (who or "").strip()
    if not who:
        return []
    if who in subs:
        return [who]
    w = who.lstrip("@").lower()
    by_user = [cid for cid, s in subs.items() if (s.get("username") or "").lower() == w]
    if by_user:
        return by_user
    return [cid for cid, s in subs.items() if (s.get("name") or "").lower().startswith(w)]


def due_notice(sub: dict, now: datetime, is_owner: bool = False) -> Optional[int]:
    """Which notice is due now, if any: 5 or 1 (days before the end) or 0 (just expired).
    Marks it sent. Never during the night, never twice for the same end date."""
    if is_owner or sub.get("blocked") or sub.get("revoked"):
        return None
    if not (NOTIFY_FROM <= now.hour < NOTIFY_UNTIL):
        return None
    end = access_until(sub)
    if end is None:
        return None
    note = sub.get("notified") or {}
    if note.get("end") != end.isoformat(timespec="seconds"):
        note = {"end": end.isoformat(timespec="seconds"), "sent": []}
    left = (end - now).total_seconds() / 86400
    due = None
    if left <= 0:
        due = 0 if 0 not in note["sent"] else None
    else:
        for d in sorted(REMIND_DAYS):                    # the most urgent unsent one
            if left <= d and d not in note["sent"]:
                due = d
                break
    if due is not None:
        note["sent"] = sorted(set(note["sent"]) | {due} | ({d for d in REMIND_DAYS if d > due} if due else set(REMIND_DAYS)))
        sub["notified"] = note
    return due
