"""Per-commit cost of an output vs the best possible (debug helper)."""
import json, sys
data, out = sys.argv[1], sys.argv[2]
T = json.load(open(data + "/tests.json"))["tests"]
A = json.load(open(data + "/answers.json"))["commits"]
ms = lambda n: round(T[n]["seconds"] * 1000)
def cost(workers):
    ws = [w for w in workers if w]
    return (max(sum(ms(n) for n in w) for w in ws) + 2000 * len(ws)) / 1000 if ws else 0
def best(f):
    if not f: return 0
    d = sorted((ms(n) for n in f), reverse=True); b = None
    for w in range(1, 9):
        L = [0] * w
        for x in d: L[L.index(min(L))] += x
        c = max(L) + 2000 * w; b = c if b is None or c < b else b
    return b / 1000
tot = lb = 0
for l in open(out):
    d = json.loads(l); c = d["commit"]; f = set(A[c]["failing"]); s = {n for w in d["workers"] for n in w}
    co = cost(d["workers"]) + (300 if f - s else 0); b = best(f); tot += co; lb += b
    extra = sorted(s - f, key=lambda n: -T[n]["seconds"])
    if abs(co - b) > 0.001 or "-a" in sys.argv: print(f"{c} {A[c]['kind']:8} cost={co:7.2f} best={b:6.2f} sel={len(s):4} fail={len(f):3} extra=" + ",".join(T[n]["id"].split("::")[-1] + f"({T[n]['seconds']:.1f})" for n in extra[:4]))
print(f"total={tot:.2f} best={lb:.2f}")
