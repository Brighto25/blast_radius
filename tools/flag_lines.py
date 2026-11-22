"""Lines in sqlglot that read an UPPER_CASE attribute (class flags/tables).
usage: python flag_lines.py <sqlglot_checkout> <out.json>   -> {"file:line": [names]}"""
import dis
import json
import os
import re
import sys
import types

repo, out = sys.argv[1], sys.argv[2]
FLAG = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
res = {}
for root, _, files in os.walk(os.path.join(repo, "sqlglot")):
    for fn in files:
        if not fn.endswith(".py"):
            continue
        path = os.path.join(root, fn)
        rel = os.path.relpath(path, repo).replace(os.sep, "/")
        try:
            top = compile(open(path, encoding="utf-8").read(), rel, "exec")
        except SyntaxError:
            continue
        stack = [top]
        while stack:
            co = stack.pop()
            for c in co.co_consts:
                if isinstance(c, types.CodeType):
                    stack.append(c)
            if co is top:
                continue
            for ins in dis.get_instructions(co):
                if ins.opname in ("LOAD_ATTR", "LOAD_METHOD", "LOAD_GLOBAL", "LOAD_NAME", "LOAD_DEREF") and isinstance(ins.argval, str):
                    if FLAG.match(ins.argval) and ins.positions and ins.positions.lineno:
                        res.setdefault(f"{rel}:{ins.positions.lineno}", set()).add(ins.argval)
json.dump({k: sorted(v) for k, v in res.items()}, open(out, "w"))
print("flag lines", len(res))
