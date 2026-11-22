"""List missed commits and the failing tests not selected. usage: misses.py <data_dir> <output.jsonl>"""
import json, sys
D, out = sys.argv[1], sys.argv[2]
A = json.load(open(f"{D}/answers.json"))["commits"]
T = json.load(open(f"{D}/tests.json"))["tests"]
for l in open(out):
    d = json.loads(l)
    sel = {n for w in d["workers"] for n in w}
    miss = set(A[d["commit"]]["failing"]) - sel
    if miss:
        print(d["commit"], A[d["commit"]]["kind"], [T[n]["id"] for n in sorted(miss)][:6])
