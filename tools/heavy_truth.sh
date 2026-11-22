#!/bin/bash
# For a batch the selector just ran on: each selected test over 5 s, does it truly fail?
T="$(cd "$(dirname "$0")/.." && pwd)"
D=${1:-$HOME/br/synth_7}
python3 - "$D" > ~/br/heavy_pairs.txt <<'PY'
import json, sys
D = sys.argv[1]
tests = json.load(open(f"{D}/tests.json"))["tests"]
for line in open(__import__("os").path.expanduser("~/br/output.jsonl")):
    o = json.loads(line)
    sel = {n for w in o.get("workers", o.get("shards", [])) for n in (w if isinstance(w, list) else w.get("tests", []))}
    for n in sel:
        if tests[n]["seconds"] > 5:
            print(o["commit"], tests[n]["id"])
PY
cat ~/br/heavy_pairs.txt
while read -r c t; do
  echo "== $c $t"
  bash "$T/tools/check_one.sh" "$D/commits/$c.patch" "$t" < /dev/null | tail -2
done < ~/br/heavy_pairs.txt
