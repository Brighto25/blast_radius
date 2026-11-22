#!/bin/bash
# Generate unlabeled synthetic commits, run the selector, list the costliest commits.
# usage: outliers.sh <count> <seed>   (no labels needed: cost = selected seconds + workers)
set -u
T="$(cd "$(dirname "$0")/.." && pwd)"
D=~/br/synth_$2
rm -rf "$D" && mkdir -p "$D"
~/br/venv/bin/python "$T/tools/gen_mutants.py" ~/br/sqlglot "$D" "$1" "$2" > /dev/null
cp "$T/kit/sample/tests.json" "$D/tests.json"
python3 - "$D" <<'PY'
import json, os, sys
D = sys.argv[1]
ids = [f[:-6] for f in os.listdir(f"{D}/commits") if f.endswith(".patch")]
json.dump({"commits": {c: {"failing": [], "kind": "?"} for c in ids}}, open(f"{D}/answers.json", "w"))
PY
BR_DATA="$D" BR_DEBUG=1 bash "$T/tools/run_sample.sh" | grep -E "Simul"
grep -o "cpu user=.*" ~/br/selector.err
python3 "$T/tools/breakdown.py" "$D" ~/br/output.jsonl | sort -t= -k2 -rn | head -15 | cut -c1-180
