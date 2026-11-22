#!/bin/bash
# Label synthetic commits by running the full suite on each patched tree.
# usage: label.sh <synth_dir>   (writes <synth_dir>/answers.json, tests.json)
set -u
T="$(cd "$(dirname "$0")/.." && pwd)"
S="$1"
export PATH=~/br/venv/bin:$PATH SKIP_INTEGRATION=0 PYTHONPATH=.:"$T/tools"
[ -d ~/br/lab ] || git clone -q ~/br/sqlglot ~/br/lab
cd ~/br/lab
cp "$T/kit/sample/tests.json" "$S/tests.json"
mkdir -p "$S/labels"
for p in "$S"/commits/*.patch; do
  id=$(basename "$p" .patch)
  [ -f "$S/labels/$id.done" ] && continue
  git checkout -q -- . && git clean -qfd -e __pycache__
  if ! git apply "$p"; then echo "$id apply failed"; continue; fi
  rm -f "$S/labels/$id".*.json
  LABELOUT="$S/labels/$id" timeout 900 python -m pytest -q -n 4 -p labelplugin -p no:cacheprovider \
     --timeout 300 tests > "$S/labels/$id.log" 2>&1
  touch "$S/labels/$id.done"
  echo "$id $(tail -1 "$S/labels/$id.log")"
done
git checkout -q -- .
python3 - "$S" <<'EOF'
import json, glob, os, sys
S = sys.argv[1]
tests = json.load(open(f"{S}/tests.json"))["tests"]
num = {t["id"]: t["number"] for t in tests}
kinds = json.load(open(f"{S}/kinds.json"))
ans = {}
for cid, k in kinds.items():
    if not os.path.exists(f"{S}/labels/{cid}.done"):
        continue
    failed = set()
    for f in glob.glob(f"{S}/labels/{cid}.*.json"):
        failed |= set(json.load(open(f)))
    nums = set()
    for n in failed:
        if n.startswith("COLLECT::"):
            mod = n[len("COLLECT::"):]
            nums |= {t["number"] for t in tests if t["id"].startswith(mod)}
        elif n in num:
            nums.add(num[n])
    ans[cid] = {"failing": sorted(nums), "kind": k["kind"]}
json.dump({"commits": ans}, open(f"{S}/answers.json", "w"))
print("labeled", len(ans))
EOF
