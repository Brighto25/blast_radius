#!/bin/bash
# Make TPC fixture mutants, run the selector on them, then find the truth for the TPC tests.
T="$(cd "$(dirname "$0")/.." && pwd)"
D=~/br/tpc; rm -rf "$D"; mkdir -p "$D"
python3 "$T/tools/tpc_mutants.py" ~/br/sqlglot "$D"
cp "$T/kit/sample/tests.json" "$D/"
python3 -c "import json,os;json.dump({'commits':{f[:-6]:{'failing':[],'kind':'fixture'} for f in os.listdir('$D/commits')}},open('$D/answers.json','w'))"
BR_DATA="$D" BR_DEBUG=1 bash "$T/tools/run_sample.sh" | grep Simul
grep -E "cands=|ran=" ~/br/selector.err | cut -c10-150
python3 "$T/tools/breakdown.py" "$D" ~/br/output.jsonl -a | cut -c1-160
export PATH=~/br/venv/bin:$PATH SKIP_INTEGRATION=0
cd ~/br/lab
for p in "$D"/commits/*.patch; do
  git checkout -q -- . && git apply "$p"
  echo "== $(basename "$p") truth:"
  python -m pytest -q -p no:cacheprovider tests/test_executor.py tests/test_optimizer.py -k "tpc" 2>&1 | grep -E "FAILED|passed|failed" | cut -c1-120
done
git checkout -q -- .
