"""
End-of-day report card: one image after the close, on X and the Telegram channel, for the Standard plan
(3/2/1 lots, ₹5 lakh capital) — the same whole-lot ledger as every other post.

    python -m publish.day_card preview [YYYY-MM-DD]   write the PNG to /tmp and print the numbers (posts nothing)
    python -m publish.day_card post                   post today's card (systemd timer, Mon–Fri 15:47 IST)

On the card: day P&L · lots · entry/exit price per trade · margin utilised (the most Zerodha margin the plan's
positions needed at any time today) and its share of ₹5 lakh · margin available · return on margin and on
capital · MTM at close plus the day's low/high from the hourly marks · margin still blocked at close.
Nothing posts on a day without a trade, or twice on the same day.
"""
from __future__ import annotations

import io
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

import requests

import paths as _paths
from publish import follow as F
from publish import x_poster as X

PLAN = "standard"
CAPITAL = 500_000
STATE: Path = _paths.LOGS_DIR / "day_card.json"
OUTBOX: Path = _paths.SIGNAL_OUTBOX
W, H = 1200, 675
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
HASHTAGS = "#Nifty #OptionSelling #AlgoTrading #TradingJournal"


# ── numbers ──────────────────────────────────────────────────────────────────

def day_events(day: str, outbox: Optional[Path] = None) -> list:
    outbox = outbox or OUTBOX
    out = []
    if outbox.exists():
        for line in outbox.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("date") == day:
                out.append(e)
    return sorted(out, key=lambda e: e["seq"])


def build(events: list, margin_of: Optional[Callable] = None) -> dict:
    """Replay the day through the plan's whole-lot ledger. margin_of(pos, lots) -> ₹ or None."""
    positions, trades, marks = {}, [], []
    booked = 0
    for ev in events:
        t, pid = ev["type"], ev.get("position_id")
        if t == "ENTRY":
            pos = F.new_position(ev)
            st = F.size_state(pos, PLAN)
            pos["_fill"] = {leg: {"in_lots": st["n"], "in_val": (pos["legs"][leg]["price"] or 0) * st["n"],
                                  "out_lots": 0, "out_val": 0.0, "out_known": 0} for leg in pos["legs"]}
            pos["_peak_lots"] = st["n"]
            pos["_opened"] = ev["ts"]
            positions[pid] = pos
        elif t in ("ADD", "EXIT") and pid in positions:
            pos = positions[pid]
            res = F.step(pos, ev)[PLAN]
            for leg, d in res["deltas"].items():
                f = pos["_fill"][leg]
                if d > 0:
                    price = next((l.get("price") for l in ev.get("legs", []) if l["leg"] == leg), None) or 0
                    f["in_lots"] += d
                    f["in_val"] += price * d
                else:
                    f["out_lots"] += -d
                    if ev.get("exit_price") and len(F.legs_of(ev)) == 1:
                        f["out_val"] += ev["exit_price"] * -d
                        f["out_known"] += -d
            pos["_peak_lots"] = max(pos["_peak_lots"], F.open_lots(pos, PLAN))
            if t == "EXIT" and ev.get("position_closed"):
                rs = round(F.realised(pos, PLAN))
                booked += rs
                legs = []
                for leg, l in pos["legs"].items():
                    f = pos["_fill"][leg]
                    legs.append({"leg": leg, "strike": l["strike"],
                                 "entry": f["in_val"] / f["in_lots"] if f["in_lots"] else None,
                                 "exit": f["out_val"] / f["out_known"] if f["out_known"] else None})
                margin = None
                if margin_of:
                    try:
                        margin = margin_of(pos, pos["_peak_lots"])
                    except Exception:                        # noqa: BLE001 — a missing margin never blocks the card
                        margin = None
                trades.append({"structure": pos["structure"], "grade": pos.get("grade"), "lots": pos["_peak_lots"],
                               "legs": legs, "rs": rs, "opened": pos["_opened"][11:16], "closed": ev["ts"][11:16],
                               "margin": margin, "hedged": bool(pos.get("hedge"))})
                positions.pop(pid)
        elif t == "PNL_SNAPSHOT":
            open_rs = 0
            for pos in positions.values():
                ov = F.open_value(pos, PLAN, ev.get("unrealised"))
                open_rs += round(F.realised(pos, PLAN)) + (round(ov) if ov is not None else 0)
            marks.append(booked + open_rs)
    marks.append(booked)                                     # the close
    margins = [t["margin"] for t in trades if t["margin"]]
    peak = max(margins) if margins else None
    return {"pnl": booked, "trades": trades, "margin_peak": peak, "capital": CAPITAL,
            "ret_margin": (booked / peak) if peak else None, "ret_capital": booked / CAPITAL,
            "mtm_low": min(marks), "mtm_high": max(marks), "mtm_close": booked,
            "blocked_at_close": 0 if not positions else None, "open_left": len(positions)}


def kite_margin(pos: dict, lots: int) -> Optional[float]:
    from publish import telegram_live as T
    return T.margin_for(pos, lots)["total"]


# ── the image ────────────────────────────────────────────────────────────────

def _font(size: int, bold: bool = False, mono: bool = False):
    from PIL import ImageFont
    name = ("DejaVuSansMono" if mono else "DejaVuSans") + ("-Bold" if bold else "") + ".ttf"
    for p in (FONT_DIR / name, Path(name)):
        try:
            return ImageFont.truetype(str(p), size)
        except OSError:
            continue
    return ImageFont.load_default()


def indian(n: float) -> str:
    """12,34,567 — Indian digit grouping."""
    s = str(int(round(abs(n))))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return ",".join(([head] if head else []) + groups + [tail])


def _rs(x: Optional[float]) -> str:
    return "—" if x is None else f"{'+' if x >= 0 else '−'}₹{indian(x)}"


def _inr(x: Optional[float]) -> str:
    return "—" if x is None else f"₹{indian(x)}"


def _pct(x: Optional[float]) -> str:
    return "—" if x is None else f"{'+' if x >= 0 else '−'}{abs(x) * 100:.2f}%"


def render(d: dict, day: date) -> bytes:
    from PIL import Image, ImageDraw
    BG, PANEL, INK, MUTED = (13, 20, 38), (24, 33, 56), (235, 240, 250), (150, 163, 190)
    GREEN, RED, ACCENT = (52, 211, 153), (248, 113, 113), (96, 165, 250)
    img = Image.new("RGB", (W, H), BG)
    g = ImageDraw.Draw(img)
    pnl_col = GREEN if d["pnl"] >= 0 else RED

    g.text((48, 34), "THETA DESK", font=_font(26, True), fill=ACCENT)
    g.text((48 + 200, 38), f"Day report · {day:%a %-d %b %Y}", font=_font(22), fill=MUTED)
    g.text((48, 78), _rs(d["pnl"]), font=_font(72, True), fill=pnl_col)
    g.text((52, 166), "Day P&L · Standard plan (3/2/1 lots) · real fills, before charges", font=_font(20), fill=MUTED)

    tiles = [("Return on margin", _pct(d["ret_margin"])), ("Return on capital", _pct(d["ret_capital"])),
             ("Margin utilised", _inr(d["margin_peak"]) + (f"  ({d['margin_peak'] / d['capital']:.0%})" if d["margin_peak"] else "")),
             ("Margin available", _inr(d["capital"])),
             ("MTM close · low / high", f"{_rs(d['mtm_close'])} · {_rs(d['mtm_low'])} / {_rs(d['mtm_high'])}"),
             ("Blocked at close", _inr(d["blocked_at_close"]) if d["blocked_at_close"] is not None else f"{d['open_left']} open")]
    tw, th = 352, 84
    for i, (label, value) in enumerate(tiles):
        x, y = 48 + (i % 3) * (tw + 24), 214 + (i // 3) * (th + 16)
        g.rounded_rectangle((x, y, x + tw, y + th), 14, fill=PANEL)
        g.text((x + 18, y + 12), label, font=_font(17), fill=MUTED)
        col = INK if "Return" not in label else (GREEN if (d["pnl"] >= 0) else RED)
        g.text((x + 18, y + 40), value, font=_font(25 if len(value) < 22 else 20, True), fill=col)

    y = 426
    g.text((48, y), "Trade", font=_font(18, True), fill=MUTED)
    for x, h in ((520, "Lots"), (620, "Entry"), (770, "Exit"), (960, "P&L")):
        g.text((x, y), h, font=_font(18, True), fill=MUTED)
    y += 32
    for t in d["trades"][:4]:
        leg_txt = " + ".join(f"{l['strike']} {l['leg']}" for l in t["legs"])
        name = ("Strangle " if t["structure"] == "STRANGLE" else "Sold ") + leg_txt
        g.text((48, y), f"{t['opened']}–{t['closed']}  {name}"[:40], font=_font(20), fill=INK)
        g.text((520, y), f"{t['lots']}" + (" each" if t["structure"] == "STRANGLE" else ""), font=_font(20), fill=INK)
        ent = " / ".join(f"{l['entry']:.2f}" if l["entry"] else "—" for l in t["legs"])
        ext = " / ".join(f"{l['exit']:.2f}" if l["exit"] else "mkt" for l in t["legs"])
        g.text((620, y), ent, font=_font(20, mono=True), fill=INK)
        g.text((770, y), ext, font=_font(20, mono=True), fill=INK)
        g.text((960, y), _rs(t["rs"]), font=_font(20, True), fill=GREEN if t["rs"] >= 0 else RED)
        y += 36
    if len(d["trades"]) > 4:
        g.text((48, y), f"+ {len(d['trades']) - 4} more trade(s)", font=_font(18), fill=MUTED)

    g.line((48, H - 64, W - 48, H - 64), fill=PANEL, width=2)
    g.text((48, H - 48), "Every trade, wins and losses: t.me/ThetaDeskIN · Not investment advice", font=_font(18), fill=MUTED)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def caption(d: dict, day: date) -> str:
    n = len(d["trades"])
    return X._fit(f"📊 Day report — {day:%a %-d %b}\n"
                  f"P&L {_rs(d['pnl'])} on the 3/2/1 plan · {n} trade{'s' if n != 1 else ''}\n"
                  f"Return on margin {_pct(d['ret_margin'])} · on ₹5L capital {_pct(d['ret_capital'])}\n{HASHTAGS}")


# ── posting ──────────────────────────────────────────────────────────────────

def post_x(png: bytes, text: str, creds: dict, http=None) -> str:
    http = http or requests
    url = "https://api.x.com/2/media/upload"
    r = http.post(url, files={"media": ("day.png", png, "image/png")},
                  data={"media_category": "tweet_image", "media_type": "image/png"}, timeout=(5, 60),
                  headers={"Authorization": X.oauth_header("POST", url, creds)})
    if r.status_code >= 300:
        raise RuntimeError(f"X media upload HTTP {r.status_code}: {r.text[:200]}")
    media_id = r.json()["data"]["id"]
    r = http.post(X.POST_URL, json={"text": text, "media": {"media_ids": [media_id]}}, timeout=(5, 30),
                  headers={"Authorization": X.oauth_header("POST", X.POST_URL, creds), "Content-Type": "application/json"})
    if r.status_code >= 300:
        raise RuntimeError(f"X post HTTP {r.status_code}: {r.text[:200]}")
    return r.json()["data"]["id"]


def post_telegram(png: bytes, text: str, http=None) -> int:
    from publish import telegram_live as T
    http = http or requests
    s = T.load_settings()
    if not s.get("live") or not s.get("channel"):
        raise RuntimeError("Telegram is not live / no channel")
    r = http.post(f"https://api.telegram.org/bot{s['token']}/sendPhoto", data={"chat_id": s["channel"],
                  "caption": text.replace("&", "&amp;"), "parse_mode": "HTML"},
                  files={"photo": ("day.png", png, "image/png")}, timeout=(5, 60))
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto: {body.get('description')}")
    return body["result"]["message_id"]


def run(day: date, do_post: bool, margin_of=kite_margin, x_post=post_x, tg_post=post_telegram) -> dict:
    events = day_events(day.isoformat())
    d = build(events, margin_of)
    out = {"day": day.isoformat(), "trades": len(d["trades"]), "pnl": d["pnl"], "margin_peak": d["margin_peak"]}
    if not do_post:
        out["png"] = render(d, day)
        return out
    if not X.is_market_day(day) or not d["trades"]:
        out["skipped"] = "no trade today" if d["trades"] == [] else "not a market day"
        return out
    try:
        st = json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        st = {}
    done = st.get(day.isoformat(), {})
    png, text = render(d, day), caption(d, day)
    creds, live = X.load_env()
    if "x" not in done and live:
        try:
            done["x"] = x_post(png, text, creds)
        except Exception as e:                               # noqa: BLE001
            out["x_error"] = str(e)
    if "tg" not in done:
        try:
            done["tg"] = tg_post(png, text)
        except Exception as e:                               # noqa: BLE001
            out["tg_error"] = str(e)
    st[day.isoformat()] = done
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, STATE)
    out.update(done)
    return out


def main(argv=None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    cmd = args[0] if args else "preview"
    day = date.fromisoformat(args[1]) if len(args) > 1 else datetime.now(X.IST).date()
    if cmd == "preview":
        out = run(day, False)
        path = Path(f"/tmp/day_card_{day.isoformat()}.png")
        path.write_bytes(out.pop("png"))
        print(json.dumps(out), "→", path)
        return 0
    if cmd == "post":
        print(json.dumps(run(day, True)))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
