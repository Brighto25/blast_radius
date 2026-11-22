#!/bin/bash
# Sample + labeled synthetic + unlabeled outlier batch, in one go.
T="$(cd "$(dirname "$0")/.." && pwd)"
echo "== sample"; BR_DEBUG=1 bash "$T/tools/run_sample.sh" | grep -E "Simul|Miss"; grep -o "cpu user=.*" ~/br/selector.err
python3 "$T/tools/breakdown.py" "$T/kit/sample" ~/br/output.jsonl | tail -1
echo "== synth (labeled)"; bash "$T/tools/eval_synth.sh" | grep -E "Simul|Miss|cpu|total"
D=~/br/synth_7
echo "== synth_7 (unlabeled)"; BR_DATA="$D" BR_DEBUG=1 bash "$T/tools/run_sample.sh" | grep Simul; grep -o "cpu user=.*" ~/br/selector.err
python3 "$T/tools/breakdown.py" "$D" ~/br/output.jsonl -a > ~/br/bd7.txt
python3 - <<'PY'
import re, os
rows=[l for l in open(os.path.expanduser("~/br/bd7.txt")) if "cost=" in l]
rows.sort(key=lambda l: -float(re.search(r"cost=\s*([\d.]+)", l).group(1)))
for l in rows[:6]: print(l[:170].rstrip())
PY
