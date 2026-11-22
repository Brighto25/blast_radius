#!/bin/bash
# Local stand-in for `make sample` (no Docker here): run the selector on the
# sample commits against the pinned checkout, then score with the kit's scorer.
set -u
TOOLS="$(cd "$(dirname "$0")" && pwd)"
ROOT="$TOOLS/.."
W=~/br
export PATH=$W/venv/bin:$PATH
export BR_REPO=$W/sqlglot BR_DATA="${BR_DATA:-$ROOT/kit/sample}" BR_MAP="$ROOT/map.json.gz" HOME=/tmp
OUT=$W/output.jsonl
/usr/bin/time -f "cpu user=%U sys=%S wall=%e maxrss=%MKB" python "$ROOT/selector.py" > $OUT 2> $W/selector.err
echo "rc=$?"
tail -3 $W/selector.err
grep -E "killed=True|status=[1-9]|failed|fatal|mismatch" $W/selector.err | head -20
python "$ROOT/kit/score.py" "$BR_DATA" "$BR_DATA/answers.json" $OUT
