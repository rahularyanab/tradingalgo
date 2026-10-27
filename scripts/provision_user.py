"""
Create a new person's folder on the shared server and show what their settings will be.

    python3 scripts/provision_user.py priya --capital 1000000
    python3 scripts/provision_user.py raj --capital 600000 --lots 1 --experience new --broker zerodha

Creates   <base>/<name>/profile.toml   (their settings, validated before anything is written)
          <base>/<name>/.env           (secrets template, owner-readable only)
          <base>/<name>/logs/  data/   (their own logs, trade state, journal, S/R levels)
and prints the exact commands to start their bot. It never touches the owner's own instance.
"""
import argparse
import os
import re
import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import userprofile                                   # noqa: E402  (does not import config)
from sizing import ProfileError                      # noqa: E402

ENV_TEMPLATE = """# SECRETS for {name} — never share, never commit. Fill every line.

# Telegram: create a bot with @BotFather (send /newbot) and paste its token. Then send any message to your bot
# and find your numeric chat id with @userinfobot.
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Zerodha: your Kite Connect app (developers.kite.trade) and your login.
KITE_API_KEY=
KITE_API_SECRET=
ZERODHA_USER_ID=
ZERODHA_PASSWORD=
ZERODHA_TOTP_SECRET=
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="short id: letters, digits, - or _ (e.g. priya)")
    ap.add_argument("--capital", type=float, required=True, help="rupees allocated to the bot")
    ap.add_argument("--broker", default="zerodha")
    ap.add_argument("--max-loss", type=float, default=None, help="daily loss limit; default 1%% of capital")
    ap.add_argument("--lots", type=int, default=None, help="preferred starting lots; default = suggested")
    ap.add_argument("--experience", choices=["new", "experienced"], default="new")
    ap.add_argument("--display-name", default=None, help="name used in Telegram messages")
    ap.add_argument("--base", default="/opt/algo-users")
    ap.add_argument("--no-seed", action="store_true", help="do not copy the owner's S/R levels as a starting point")
    ap.add_argument("--force", action="store_true", help="overwrite an existing profile.toml (never touches .env)")
    a = ap.parse_args(argv)

    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,29}", a.name):
        print("Name must be 2-30 characters: lowercase letters, digits, - or _ .", file=sys.stderr)
        return 2
    d = Path(a.base) / a.name
    prof = d / "profile.toml"
    if prof.exists() and not a.force:
        print(f"{prof} already exists. Use --force to rewrite the profile (the .env is never overwritten).", file=sys.stderr)
        return 2

    text = userprofile.render_template(
        a.display_name or a.name.title(), a.broker.lower(), a.capital, a.max_loss, a.experience, a.lots)
    try:                                              # validate BEFORE creating anything
        import importlib.util
        cfg_ns = {"MAX_START_LOTS": 3}
        st = userprofile.build(userprofile.parse(text), cfg_ns, text)
    except ProfileError as e:
        print(f"Cannot create this profile:\n{e}", file=sys.stderr)
        return 1

    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, stat.S_IRWXU)
    (d / "logs").mkdir(exist_ok=True)
    (d / "data").mkdir(exist_ok=True)
    prof.write_text(text)
    env = d / ".env"
    if not env.exists():
        env.write_text(ENV_TEMPLATE.format(name=a.name))
    os.chmod(env, stat.S_IRUSR | stat.S_IWUSR)        # 600
    seed = ROOT / "data" / "sr_levels.json"
    if not a.no_seed and seed.exists() and not (d / "data" / "sr_levels.json").exists():
        shutil.copy(seed, d / "data" / "sr_levels.json")

    z = st.sizing
    print(f"""
Created {d}

  Capital            ₹{z.capital:,.0f}
  Daily loss limit   ₹{z.daily_limit:,.0f}  ({z.limit_pct:.1f}% of capital)
  Starting lots      {z.lots_strong} strong / {z.lots_moderate} moderate   (suggested {z.suggested_lots}; risk rule {z.risk_lots}, margin rule {z.margin_lots})
  Strangle           {z.lots_strangle} lot(s) each side
  Grows only in profit up to {z.max_lots_directional} lots
  Margin at start    about ₹{z.margin_at_start:,.0f}  ({100 * z.margin_at_start / z.capital:.0f}% of capital)""")
    for n in z.notes:
        print(f"  note: {n}")
    for w in z.warnings:
        print(f"  WARNING: {w}")
    print(f"""
Next:
  1. Fill in {env}   (Telegram bot token + chat id, broker keys)
  2. Preview:  ALGO_USER_DIR={d} python3 scripts/check_profile.py
  3. Start:    systemctl enable --now trading-bot@{a.name}
               systemctl enable --now trading-login@{a.name}.timer     # daily broker login at 8:45 AM IST
  The Telegram guide is sent automatically when the bot first connects.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
