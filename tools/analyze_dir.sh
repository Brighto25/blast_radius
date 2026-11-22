#!/bin/bash
# Analysis-only pass (no test runs) over a commits dir: candidate sizes per commit.
T="$(cd "$(dirname "$0")/.." && pwd)"
D=${1:-/home/bright/br/synth}
cp "$T/kit/sample/tests.json" "$D/tests.json" 2>/dev/null
cd ~/br && BR_ANALYZE_ONLY=1 BR_DEBUG=1 BR_REPO=/home/bright/br/sqlglot BR_DATA="$D" BR_MAP="$T/map.json.gz" \
  venv/bin/python "$T/selector.py" 2>&1 >/dev/null | grep "cands=" | cut -c10-260
