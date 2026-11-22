#!/bin/bash
# Score the selector on the labeled synthetic commits (+ optional sample).
set -u
T="$(cd "$(dirname "$0")/.." && pwd)"
S=${1:-/home/bright/br/synth}
E=/home/bright/br/synth_eval
rm -rf "$E" && mkdir -p "$E/commits"
python3 - "$S" "$E" <<'EOF'
import json, shutil, sys
S, E = sys.argv[1], sys.argv[2]
a = json.load(open(f"{S}/answers.json"))["commits"]
for c in a:
    shutil.copy(f"{S}/commits/{c}.patch", f"{E}/commits/{c}.patch")
json.dump({"commits": a}, open(f"{E}/answers.json", "w"))
shutil.copy(f"{S}/tests.json", f"{E}/tests.json")
print("commits", len(a))
EOF
BR_DATA="$E" BR_DEBUG=1 bash "$T/tools/run_sample.sh" | grep -E "Simul|Miss|missed"
grep -o "cpu user=.*" ~/br/selector.err
python3 "$T/tools/breakdown.py" "$E" ~/br/output.jsonl | cut -c1-200
