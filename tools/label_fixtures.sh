#!/bin/bash
# Label only the fixture-kind commits of a synthetic batch (plus the TPC mutants) into ~/br/synth_fx.
T="$(cd "$(dirname "$0")/.." && pwd)"
S=${1:-$HOME/br/synth_7}; D=~/br/synth_fx
mkdir -p "$D/commits"
python3 - "$S" "$D" <<'PY'
import json, shutil, sys, os
S, D = sys.argv[1], sys.argv[2]
k = json.load(open(f"{S}/kinds.json"))
out = {}
for c, v in k.items():
    if v["kind"] == "fixture":
        shutil.copy(f"{S}/commits/{c}.patch", f"{D}/commits/{c}.patch"); out[c] = v
for f in os.listdir(os.path.expanduser("~/br/tpc/commits")):
    shutil.copy(os.path.expanduser(f"~/br/tpc/commits/{f}"), f"{D}/commits/{f}"); out[f[:-6]] = {"kind": "fixture"}
json.dump(out, open(f"{D}/kinds.json", "w"))
print("fixture commits", len(out))
PY
bash "$T/tools/label.sh" "$D"
