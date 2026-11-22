"""More edge-case commits: annotations, imports, new/removed defs, renames."""
import difflib, os, re, sys
repo, out = sys.argv[1], sys.argv[2]
os.makedirs(out + "/commits", exist_ok=True)

def edit(cid, rel, fn):
    src = open(os.path.join(repo, rel), encoding="utf-8").read()
    new = fn(src)
    if new == src:
        print(cid, "NO CHANGE"); return
    d = "\n".join(difflib.unified_diff(src.split("\n"), new.split("\n"), f"a/{rel}", f"b/{rel}", n=3, lineterm=""))
    open(f"{out}/commits/{cid}.patch", "w", newline="\n").write(f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n{d}\n")

def sub1(pat, rep):
    return lambda s: re.sub(pat, rep, s, count=1, flags=re.M)

edit("f01", "sqlglot/optimizer/scope.py", sub1(r"(def \w+\(self)\)( -> )", r"\1)\2"))  # placeholder
edit("f02", "sqlglot/parser.py", sub1(r"^(import typing as t)$", r"\1\nimport itertools as _it"))
edit("f03", "sqlglot/optimizer/simplify.py", lambda s: s + "\n\ndef _unused_helper(x):\n    return x\n")
edit("f04", "sqlglot/generator.py", sub1(r"-> str:\n", r"-> 'str':\n"))
edit("f05", "sqlglot/expressions/core.py", sub1(r"(\n    def \w+\(self)(\) -> )([\w\.]+)(:)", r"\1\2t.Any\4"))
edit("f06", "sqlglot/tokens.py", sub1(r"^(from __future__ import annotations)$", r"\1\n"))
edit("f07", "sqlglot/dialects/duckdb.py", sub1(r"(class DuckDB\(Dialect\):\n)", r"\1    _UNUSED_FLAG = 1\n"))
edit("f08", "sqlglot/optimizer/qualify_columns.py", sub1(r"(\n)(def _qualify_columns)", r"\1def _unused2():\n    pass\n\n\n\2"))
print("done", len(os.listdir(out + "/commits")))
