"""
Dry-run for a person's setup — sends nothing, places nothing.

    ALGO_USER_DIR=/opt/algo-users/priya python3 scripts/check_profile.py

Checks profile.toml (every problem listed in plain language), shows the sizing it produces,
reports any secrets missing from the .env, and prints the Telegram guide exactly as the user will see it.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if not os.environ.get("ALGO_USER_DIR"):
    print("Set ALGO_USER_DIR to the person's folder, e.g.\n  ALGO_USER_DIR=/opt/algo-users/priya python3 scripts/check_profile.py", file=sys.stderr)
    sys.exit(2)

import paths                                         # noqa: E402
from sizing import ProfileError                      # noqa: E402

try:
    import config                                    # noqa: E402  (loads this person's .env and profile)
except ProfileError as e:
    print(f"\n❌ {e}\n", file=sys.stderr)
    sys.exit(1)

import userprofile                                   # noqa: E402
from notifications import onboarding                 # noqa: E402

st = config.SETTINGS
z = st.sizing
print(f"\n✅ profile OK — {st.name} ({st.broker}) in {paths.USER_DIR}\n")
print(f"  capital ₹{z.capital:,.0f} | daily limit ₹{z.daily_limit:,.0f} ({z.limit_pct:.1f}%) | lots {z.lots_strong}/{z.lots_moderate} "
      f"(suggested {z.suggested_lots}: risk rule {z.risk_lots}, margin rule {z.margin_lots}) | grows to {z.max_lots_directional}")
print(f"  loss/lot assumed ₹{z.loss_per_lot:,.0f} | margin/lot assumed ₹{z.margin_per_lot:,.0f} | margin at start ₹{z.margin_at_start:,.0f}")
print(f"  rupee thresholds for this size: {z.thresholds}")
for w in z.warnings:
    print(f"  ⚠️  {w}")

miss = userprofile.missing_secrets(st.broker, os.environ)
print("\n" + (f"❌ missing secrets in {paths.ENV_FILE}: {', '.join(miss)}" if miss else "✅ all required secrets are filled in"))
print("\n──────── Telegram guide preview (not sent) ────────")
for m in onboarding.welcome_messages(st):
    print(m + "\n" + "─" * 50)
sys.exit(1 if miss else 0)
