#!/usr/bin/env bash
# Copy the executor from this (private, strategy) repo into the subscriber repo thetadesk-executor.
# This repo's executor/ is the source of truth: its tests also exercise the server side end to end.
#
#   bash scripts/sync_executor_repo.sh [path/to/thetadesk-executor]     (default: ../thetadesk-executor)
#
# Copies ONLY the executor and the Angel connection files — never strategy, signals, publish, main.py.
# Then runs the subscriber repo's own tests and shows what changed; committing/pushing stays manual.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$HERE/../thetadesk-executor}"
[ -d "$DEST/.git" ] || { echo "not a git repo: $DEST"; exit 1; }

for f in __init__.py __main__.py api.py config.py engine.py venue.py; do
  cp "$HERE/executor/$f" "$DEST/executor/$f"
done
cp "$HERE/executor/executor.env.example" "$DEST/executor.env.example"
for f in angel.py angel_instruments.py angel_paper.py; do
  cp "$HERE/broker/$f" "$DEST/broker/$f"
done

# guard: nothing from the strategy side may ever be imported by the subscriber repo
if grep -rnE "^\s*(from|import)\s+(strategy|signals|publish|main|config|notifications|data|execution)\b" \
     "$DEST/executor" "$DEST/broker" "$DEST/paths.py"; then
  echo "✗ the subscriber repo imports strategy-side modules — fix before committing"; exit 1
fi

(cd "$DEST" && python3 -I tests/test_executor.py | tail -1)
(cd "$DEST" && git status --short)
echo "Synced. Review, then commit and push in $DEST."
