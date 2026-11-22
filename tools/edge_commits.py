"""Hand-made edge-case commits (docstrings, class/module comments, enum adds, new files...) for analysis checks."""
import difflib, os, re, sys
repo, out = sys.argv[1], sys.argv[2]
os.makedirs(out + "/commits", exist_ok=True)

def diff(rel, a, b, cid):
    d = "\n".join(difflib.unified_diff(a.split("\n"), b.split("\n"), f"a/{rel}", f"b/{rel}", n=3, lineterm=""))
    open(f"{out}/commits/{cid}.patch", "w", newline="\n").write(f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n{d}\n")

def edit(cid, rel, fn):
    src = open(os.path.join(repo, rel), encoding="utf-8").read()
    new = fn(src)
    if new == src:
        print(cid, "NO CHANGE", rel); return
    diff(rel, src, new, cid)

def first_docstring_class(src, word="the"):
    # change one word inside the first triple-quoted docstring after a 'class ' line
    m = re.search(r'class \w+[^\n]*:\n\s+"""(.*?)"""', src, re.S)
    if not m: return src
    s, e = m.span(1)
    return src[:s] + src[s:e].replace(" ", "  ", 1) + src[e:]

def module_docstring(src):
    m = re.match(r'(\s*)"""(.*?)"""', src, re.S)
    if not m: return src
    s, e = m.span(2)
    return src[:s] + src[s:e].replace(" ", "  ", 1) + src[e:]

def func_docstring(src):
    m = re.search(r'def \w+\([^)]*\)[^\n]*:\n\s+"""(.*?)"""', src, re.S)
    if not m: return src
    s, e = m.span(1)
    return src[:s] + src[s:e].replace(" ", "  ", 1) + src[e:]

def class_comment(src):
    m = re.search(r'\nclass \w+[^\n]*:\n', src)
    return src[:m.end()] + "    # note\n" + src[m.end():] if m else src

edit("e01", "sqlglot/expressions/core.py", first_docstring_class)
edit("e02", "sqlglot/optimizer/scope.py", module_docstring)
edit("e03", "sqlglot/tokens.py", first_docstring_class)
edit("e04", "sqlglot/parser.py", func_docstring)
edit("e05", "sqlglot/dialects/dialect.py", first_docstring_class)
edit("e06", "sqlglot/generator.py", class_comment)
edit("e07", "sqlglot/optimizer/qualify_columns.py", module_docstring)
edit("e08", "sqlglot/expressions/core.py", class_comment)
edit("e09", "sqlglot/dialects/duckdb.py", first_docstring_class)
edit("e10", "sqlglot/executor/python.py", func_docstring)
print("done", len(os.listdir(out + "/commits")))
