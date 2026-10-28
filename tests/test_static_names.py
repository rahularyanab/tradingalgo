"""
Static guard against the bug that cost a morning on 2026-10-28: scripts/auto_login.py called load_instance_env() on line 27 and imported it
on line 32 — a NameError that only appears when the 08:45 timer runs it. pyflakes finds undefined / used-before-import names without running anything.

    python3 tests/test_static_names.py        (skips with a note if pyflakes is not installed)
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   {extra}"))


def problems(paths):
    r = subprocess.run([sys.executable, "-m", "pyflakes", *map(str, paths)], capture_output=True, text=True)
    if "No module named pyflakes" in r.stderr:
        return None
    return [l for l in (r.stdout + r.stderr).splitlines() if "undefined name" in l or "referenced before assignment" in l or "before assignment" in l]


files = [ROOT / f for f in subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True).stdout.split()
         if not f.startswith(("backtest/", "paper_trade/"))]
found = problems(files)
if found is None:
    print("  SKIP  pyflakes is not installed (pip install pyflakes) — nothing checked")
    sys.exit(0)
check(f"no undefined names in {len(files)} tracked python files", not found, "\n" + "\n".join(found[:8]))

import tempfile
bad = Path(tempfile.mkdtemp()) / "bad_script.py"
bad.write_text("from pathlib import Path\n\nload_instance_env()\nfrom paths import load_instance_env\n")
check("the checker really catches a call placed before its import (the 2026-10-28 bug)", bool(problems([bad])))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
