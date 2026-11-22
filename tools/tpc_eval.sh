#!/bin/bash
# Selector on the TPC fixture mutants (made by tpc_check.sh) and on the sample.
T="$(cd "$(dirname "$0")/.." && pwd)"
D=~/br/tpc
BR_DATA="$D" BR_DEBUG=1 bash "$T/tools/run_sample.sh" | grep Simul
grep -o "cpu user=.*" ~/br/selector.err
grep -E "ran=" ~/br/selector.err | cut -c10-150
python3 "$T/tools/breakdown.py" "$D" ~/br/output.jsonl -a | cut -c1-170
echo "== sample"; BR_DEBUG=1 bash "$T/tools/run_sample.sh" | grep -E "Simul|Miss"; grep -o "cpu user=.*" ~/br/selector.err
python3 "$T/tools/breakdown.py" "$T/kit/sample" ~/br/output.jsonl | cut -c1-170
