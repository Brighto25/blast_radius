#!/bin/bash
# Rebuild map.json.gz from existing traces, then run + score the sample.
T="$(cd "$(dirname "$0")/.." && pwd)"
cd ~/br/sqlglot
CPUOUT=~/br/cpuout ~/br/venv/bin/python "$T/tools/build_map.py" ~/br/mapout ~/br/imp/import_trace.json ~/br/sqlglot "$T/kit/sample/tests.json" "$T/map.json.gz" || exit 1
BR_DEBUG=1 bash "$T/tools/run_sample.sh" | grep -E "Simul|Miss|missed"
grep -o "cpu user=.*" ~/br/selector.err
grep -E "ran=" ~/br/selector.err | cut -c10-130 | sort -t= -k5 -rn | head -12
python3 "$T/tools/breakdown.py" "$T/kit/sample" ~/br/output.jsonl | tail -1
