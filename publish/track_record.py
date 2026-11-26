"""
The public track record: every real trade of the Nifty algo, normalised PER LOT, published as a Telegraph
page (telegra.ph — Telegram's own publishing tool: free, no domain, opens instantly inside Telegram) and
refreshed automatically after each trading day.

    python -m publish.track_record preview      print the page as text (publishes nothing)
    python -m publish.track_record publish      create the page the first time, update it afterwards
    python -m publish.track_record update       update only if it was already published (the daily timer)

Honest by construction:
  * built only from the owner's trade journal (real fills), wins and losses alike, nothing filtered;
  * per lot = a trade's net ₹ (hedge included) ÷ the lots it held, so position size never flatters it;
  * before brokerage and taxes, and it says so; it shows the worst trade and the worst drawdown.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests

import paths as _paths

IST = ZoneInfo("Asia/Kolkata")
LOT = 65
JOURNAL: Path = _paths.JOURNAL_FILE
STATE: Path = _paths.LOGS_DIR / "telegraph.json"
API = "https://api.telegra.ph"
TITLE = "Theta Desk — live track record"
AUTHOR = "Theta Desk"
AUTHOR_URL = "https://t.me/ThetaDeskIN"


# ── numbers ──────────────────────────────────────────────────────────────────

def load_trades(journal: Optional[Path] = None) -> list:
    """Journal rows are exit slices; slices of one trade share date, entry time, action, strike and expiry."""
    journal = journal or JOURNAL
    trades: "OrderedDict[tuple, dict]" = OrderedDict()
    with open(journal) as f:
        for r in csv.DictReader(f):
            try:
                lots = float(r["lots"]) / LOT
                pnl = float(r["net_pnl"])
            except (KeyError, ValueError):
                continue
            if lots <= 0:
                continue
            key = (r["date"], r.get("entry_time", ""), r["action"], r["strike"], r.get("expiry", ""))
            t = trades.setdefault(key, {"date": r["date"], "entry": r.get("entry_time", ""), "action": r["action"],
                                        "strike": r["strike"], "pnl": 0.0, "lots": 0.0, "exit": "", "reasons": []})
            t["pnl"] += pnl
            t["lots"] += lots
            t["exit"] = r.get("exit_time", t["exit"])
            if r.get("exit_reason") and r["exit_reason"] not in t["reasons"]:
                t["reasons"].append(r["exit_reason"])
    out = list(trades.values())
    for t in out:
        t["per_lot"] = t["pnl"] / t["lots"]
    return out


def stats(trades: list) -> dict:
    if not trades:
        return {"n": 0}
    pl = [t["per_lot"] for t in trades]
    wins, losses = [x for x in pl if x > 0], [x for x in pl if x <= 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in pl:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    months: "OrderedDict[str, dict]" = OrderedDict()
    days: dict = {}
    for t in trades:
        m = months.setdefault(t["date"][:7], {"pnl": 0.0, "n": 0, "wins": 0})
        m["pnl"] += t["per_lot"]
        m["n"] += 1
        m["wins"] += t["per_lot"] > 0
        days[t["date"]] = days.get(t["date"], 0.0) + t["per_lot"]
    return {"n": len(pl), "wins": len(wins), "win_rate": len(wins) / len(pl), "total": sum(pl),
            "avg_win": sum(wins) / len(wins) if wins else 0.0, "avg_loss": sum(losses) / len(losses) if losses else 0.0,
            "best": max(pl), "worst": min(pl), "max_dd": dd,
            "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
            "first": trades[0]["date"], "last": trades[-1]["date"], "months": months,
            "days": len(days), "green_days": sum(1 for v in days.values() if v > 0), "worst_day": min(days.values())}


# ── page ─────────────────────────────────────────────────────────────────────

def _rs(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}₹{abs(x):,.0f}"


def _d(s: str) -> str:
    return datetime.strptime(s, "%Y-%m-%d").strftime("%-d %b %Y")


def nodes(trades: list, now: datetime) -> list:
    s = stats(trades)
    if not s["n"]:
        return [{"tag": "p", "children": ["No trades recorded yet."]}]
    action = {"CALL_SELL": "Sold call", "PUT_SELL": "Sold put", "STRANGLE": "Strangle",
              "STRANGLE_CE": "Strangle (call leg)", "STRANGLE_PE": "Strangle (put leg)"}
    pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] else "—"
    month_rows = ["Month      Trades  Won   Per lot"] + [
        f"{datetime.strptime(k, '%Y-%m').strftime('%b %Y'):<10} {m['n']:>6}  {m['wins']:>3}  {_rs(m['pnl']):>9}"
        for k, m in s["months"].items()]
    last = trades[-25:][::-1]
    trade_rows = ["Date        Trade                     Per lot"] + [
        f"{datetime.strptime(t['date'], '%Y-%m-%d').strftime('%d %b'):<11} "
        f"{(action.get(t['action'], t['action']) + ' ' + t['strike']):<25} {_rs(t['per_lot']):>8}" for t in last]
    return [
        {"tag": "p", "children": [
            "Every trade of our fully automated Nifty option-selling algo since ", {"tag": "b", "children": [_d(s["first"])]},
            " — taken from the real trade journal, wins and losses alike, nothing left out. "
            "Updated automatically after every trading day."]},
        {"tag": "h3", "children": ["Summary (per lot)"]},
        {"tag": "ul", "children": [
            {"tag": "li", "children": [f"Trades: {s['n']} · won {s['wins']} ({s['win_rate']:.0%})"]},
            {"tag": "li", "children": [{"tag": "b", "children": [f"Total: {_rs(s['total'])} per lot"]}]},
            {"tag": "li", "children": [f"Average win {_rs(s['avg_win'])} · average loss {_rs(s['avg_loss'])} · profit factor {pf}"]},
            {"tag": "li", "children": [f"Best trade {_rs(s['best'])} · worst trade {_rs(s['worst'])}"]},
            {"tag": "li", "children": [f"Worst drawdown {_rs(s['max_dd'])} per lot · worst day {_rs(s['worst_day'])}"]},
            {"tag": "li", "children": [f"Trading days: {s['days']} · green days: {s['green_days']}"]},
        ]},
        {"tag": "h3", "children": ["Month by month"]},
        {"tag": "pre", "children": ["\n".join(month_rows)]},
        {"tag": "h3", "children": ["Latest trades"]},
        {"tag": "pre", "children": ["\n".join(trade_rows)]},
        {"tag": "h3", "children": ["How to read this"]},
        {"tag": "ul", "children": [
            {"tag": "li", "children": ["“Per lot” = a trade's net result (including the hedge) divided by the lots it held "
                                       "(1 lot = 65 Nifty). Multiply by your own lots."]},
            {"tag": "li", "children": ["Before brokerage, taxes and slippage. The rules of the algo have been refined over this period."]},
            {"tag": "li", "children": ["Live trades are posted as they happen at ", {"tag": "a", "attrs": {"href": AUTHOR_URL},
                                                                                       "children": ["t.me/ThetaDeskIN"]},
                                       " and on X @ThetaDeskIN."]},
            {"tag": "li", "children": ["Past results don't guarantee future ones. Not investment advice."]},
        ]},
        {"tag": "p", "children": [{"tag": "i", "children": [f"Last updated {now:%-d %b %Y, %H:%M} IST."]}]},
    ]


def as_text(content: list, depth: int = 0) -> str:
    """Plain-text rendering of Telegraph nodes, for the preview."""
    out = []
    for n in content:
        if isinstance(n, str):
            out.append(n)
            continue
        inner = as_text(n.get("children", []), depth + 1)
        tag = n.get("tag")
        if tag == "h3":
            out.append(f"\n## {inner}\n")
        elif tag == "li":
            out.append(f"• {inner}\n")
        elif tag in ("p", "pre"):
            out.append(f"{inner}\n" + ("\n" if tag == "p" else ""))
        else:
            out.append(inner)
    return "".join(out)


# ── Telegraph ────────────────────────────────────────────────────────────────

def _call(method: str, http=None, **params) -> dict:
    http = http or requests
    r = http.post(f"{API}/{method}", data={k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in params.items()},
                  timeout=(5, 30))
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegraph {method}: {body.get('error')}")
    return body["result"]


def publish(content: list, http=None, create: bool = True) -> Optional[str]:
    """Create (first time, only if create=True) or update the page. Returns its URL, or None if not published yet."""
    try:
        st = json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        st = {}
    if not st.get("access_token"):
        if not create:
            return None
        acc = _call("createAccount", http, short_name="ThetaDesk", author_name=AUTHOR, author_url=AUTHOR_URL)
        st = {"access_token": acc["access_token"]}
    common = dict(access_token=st["access_token"], title=TITLE, author_name=AUTHOR, author_url=AUTHOR_URL,
                  content=content, return_content="false")
    page = _call(f"editPage/{st['path']}", http, **common) if st.get("path") else _call("createPage", http, **common)
    st.update(path=page["path"], url=page["url"])
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)
    return page["url"]


def main(argv=None) -> int:
    cmd = (argv or sys.argv[1:] or ["preview"])[0]
    now = datetime.now(IST)
    content = nodes(load_trades(), now)
    if cmd == "preview":
        print(TITLE + "\n" + "=" * len(TITLE) + "\n" + as_text(content))
        return 0
    if cmd in ("publish", "update"):
        url = publish(content, create=(cmd == "publish"))
        print(url or "not published yet — run `publish` once to create the page")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
