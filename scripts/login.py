"""
Daily broker login for THIS instance, whichever broker its profile names.
(The owner's own Zerodha bot keeps using scripts/auto_login.py directly.)

    ALGO_USER_DIR=/opt/algo-users/priya python3 scripts/login.py
"""
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config                                        # noqa: E402  (loads this person's .env and profile)

if config.BROKER == "angel":
    from auth import angel_login                     # noqa: E402
    sys.exit(angel_login.run())
runpy.run_path(str(ROOT / "scripts" / "auto_login.py"), run_name="__main__")
