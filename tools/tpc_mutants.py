"""A few fixture mutants in the TPC-H / TPC-DS query files (same mutation as gen_mutants)."""
import difflib, os, random, re, sys
repo, out = sys.argv[1], sys.argv[2]
rng = random.Random(11)
os.makedirs(out + "/commits", exist_ok=True)
for k, rel in enumerate(["tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"] * 3 + ["tests/fixtures/optimizer/tpc-h/tpc-h.sql"] * 3):
    src = open(os.path.join(repo, rel), encoding="utf-8").read()
    lines = src.split("\n")
    cand = [i for i, l in enumerate(lines) if l.strip() and not l.startswith(("#", "--")) and len(l) > 20]
    i = rng.choice(cand)
    toks = list(re.finditer(r"\b[a-z]\w*\b", lines[i]))
    if not toks:
        continue
    m = rng.choice(toks)
    lines[i] = lines[i][: m.start()] + m.group(0)[::-1] + "x" + lines[i][m.end():]
    d = "\n".join(difflib.unified_diff(src.split("\n"), lines, f"a/{rel}", f"b/{rel}", n=3, lineterm=""))
    open(f"{out}/commits/t{k}.patch", "w", newline="\n").write(f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n{d}\n")
    print(f"t{k}", rel.split("/")[-1], "line", i + 1, repr(lines[i].strip()[:70]))
