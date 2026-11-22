"""Generate synthetic commits (small mutations like the sample's kinds) for local
validation only. Labels come from actually running the test suite (label.sh).

usage: python gen_mutants.py <sqlglot_checkout> <out_dir> <count> <seed>
"""

import ast
import difflib
import json
import os
import random
import re
import sys

repo, out, count, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)

py_files = []
for root, _, files in os.walk(os.path.join(repo, "sqlglot")):
    for f in files:
        if f.endswith(".py"):
            p = os.path.join(root, f)
            py_files.append(os.path.relpath(p, repo).replace(os.sep, "/"))
fixtures = []
for root, _, files in os.walk(os.path.join(repo, "tests", "fixtures")):
    for f in files:
        if f.endswith(".sql") and os.path.getsize(os.path.join(root, f)) < 400_000:
            fixtures.append(os.path.relpath(os.path.join(root, f), repo).replace(os.sep, "/"))

# weight files by size so core files get their share
weights = [os.path.getsize(os.path.join(repo, f)) ** 0.5 for f in py_files]

SWAPS = [
    (r"\band\b", "or"), (r"\bor\b", "and"), (r" == ", " != "), (r" != ", " == "),
    (r"\bis not\b", "is"), (r" is (?!not)", " is not "), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"),
    (r" < ", " <= "), (r" > ", " >= "), (r" <= ", " < "), (r" >= ", " > "), (r"\bnot ", ""),
    (r"\+ 1\b", "- 1"), (r"- 1\b", "+ 1"), (r"\[0\]", "[1]"), (r"\[1\]", "[0]"), (r"\[-1\]", "[0]"),
    (r"\bif not ", "if "), (r", 0\)", ", 1)"), (r", 1\)", ", 2)"), (r"\bNone\b", "''"),
    (r'"([A-Z_]{2,})"', r'"\1X"'), (r"\breturn ", "return None or "),
]


def code_lines(src):
    """line numbers of executable statement lines, split into function-body vs class/module level."""
    tree = ast.parse(src)
    body, top = set(), set()

    def walk(node, in_func):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.stmt):
                rng_ = range(ch.lineno, (ch.end_lineno or ch.lineno) + 1)
                if not isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    (body if in_func else top).update(rng_)
            walk(ch, in_func or isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)))

    walk(tree, False)
    return sorted(body), sorted(top - body)


def mutate_py(rel, kind):
    src = open(os.path.join(repo, rel), encoding="utf-8").read()
    lines = src.split("\n")
    try:
        body, top = code_lines(src)
    except SyntaxError:
        return None
    if kind == "comment":
        cands = body or top
        if not cands:
            return None
        ln = rng.choice(cands) - 1
        indent = re.match(r"\s*", lines[ln]).group(0)
        new = lines[:ln] + [indent + rng.choice(["# Cheap check first", "# TODO: revisit", "# Intentionally not cached"])] + lines[ln:]
        out_src = "\n".join(new)
        try:
            ast.parse(out_src)
        except SyntaxError:
            return None
        return src, out_src
    pool = body if kind == "code" else top
    for _ in range(60):
        if not pool:
            return None
        ln = rng.choice(pool) - 1
        line = lines[ln]
        if line.strip().startswith("#") or '"""' in line:
            continue
        opts = [(p, r) for p, r in SWAPS if re.search(p, line)]
        if not opts:
            m = re.search(r"\b(\d+)\b", line)
            if m and kind == "struct":
                opts = [(r"\b%s\b" % m.group(1), str(int(m.group(1)) + 1))]
            else:
                continue
        p, r = rng.choice(opts)
        matches = list(re.finditer(p, line))
        mm = rng.choice(matches)
        newline = line[: mm.start()] + re.sub(p, r, mm.group(0)) + line[mm.end():]
        if newline == line:
            continue
        new = lines[:]
        new[ln] = newline
        out_src = "\n".join(new)
        try:
            if ast.dump(ast.parse(out_src)) == ast.dump(ast.parse(src)):
                continue
        except SyntaxError:
            continue
        return src, out_src
    return None


def mutate_fixture(rel):
    src = open(os.path.join(repo, rel), encoding="utf-8").read()
    lines = src.split("\n")
    cand = [i for i, l in enumerate(lines) if l.strip() and not l.startswith("#") and not l.startswith("--") and len(l) > 20]
    if not cand:
        return None
    i = rng.choice(cand)
    l = lines[i]
    toks = [m for m in re.finditer(r"\b[a-z]\w*\b", l)]
    if not toks:
        return None
    m = rng.choice(toks)
    lines[i] = l[: m.start()] + m.group(0)[::-1] + "x" + l[m.end():]
    return src, "\n".join(lines)


def udiff(rel, a, b):
    d = difflib.unified_diff(a.split("\n"), b.split("\n"), f"a/{rel}", f"b/{rel}", n=3, lineterm="")
    body = "\n".join(d)
    return f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n" + body + "\n"


made = 0
kinds = {}
while made < count:
    r = rng.random()
    if r < 0.55:
        kind, rel = "code", rng.choices(py_files, weights)[0]
        res = mutate_py(rel, "code")
    elif r < 0.72:
        kind, rel = "struct", rng.choices(py_files, weights)[0]
        res = mutate_py(rel, "struct")
    elif r < 0.82:
        kind, rel = "comment", rng.choices(py_files, weights)[0]
        res = mutate_py(rel, "comment")
    else:
        kind, rel = "fixture", rng.choice(fixtures)
        res = mutate_fixture(rel)
    if not res:
        continue
    cid = "s%03d" % (made + seed * 1000 % 997)
    with open(os.path.join(out, "commits", cid + ".patch"), "w", encoding="utf-8", newline="\n") as f:
        f.write(udiff(rel, *res))
    kinds[cid] = {"kind": kind, "file": rel}
    made += 1
json.dump(kinds, open(os.path.join(out, "kinds.json"), "w"), indent=1)
print("made", made)
