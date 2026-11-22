#!/usr/bin/env python3
"""Blast Radius selector.

For each patch:
  1. Work out what really changed (AST level: comment/whitespace-only edits are no-ops).
  2. Map the change to candidate tests with a map built from sqlglot's own tests at
     the pinned commit (function-entry coverage, fixture reads, name references).
  3. Run the cheap candidates here, on the patched code, in a forked child, and
     keep only the ones that really fail. Expensive candidates are selected unrun.
  4. Pack the selection onto workers to minimise busiest-worker + 2 s/worker.

Nothing is looked up by patch: every decision comes from the change itself, the
map of the pinned commit, and actually executing the tests.
"""

import ast
import builtins
import copy
import gc
import gzip
import importlib
import importlib.abc
import importlib.util
import io
import json
import os
import re
import select
import signal
import sys
import time
import types

APP = os.environ.get("BR_APP", "/app")
REPO = os.path.abspath(os.environ.get("BR_REPO", os.path.join(APP, "sqlglot")))
DATA = os.environ.get("BR_DATA", "/data")
MAP_PATH = os.environ.get("BR_MAP", os.path.join(APP, "map.json.gz"))
LINES_PATH = os.environ.get("BR_LINES", os.path.join(os.path.dirname(MAP_PATH), "lines.json.gz"))
LINE_FILTER = os.environ.get("BR_LINE_FILTER", "1") == "1"
JOB_FILTER = os.environ.get("BR_JOB_FILTER", "1") == "1"
# the parser node-limit stress test, when stood in for, is checked at a smaller width
# (node limit scaled by the same ratio) after a cheap test fails
SCALED = os.environ.get("BR_SCALED", "1") == "1"
# after a cheap failure, the TPC-DS executor test stays unselected when a stand-in (parse, pickle,
# optimize, plan, duckdb transpile of every 4th TPC-DS query) matches the unpatched code exactly
STANDIN = os.environ.get("BR_STANDIN", "1") == "1"
GAMBLE = os.environ.get("BR_GAMBLE", "1") == "1"
GAMBLE_S = float(os.environ.get("BR_GAMBLE_S", "1.0"))
_STANDIN_KEY = 10**9
_TPC_REF = [None]
JOB_SETUP = float(os.environ.get("BR_JOB_SETUP", "2.5"))  # class setup + building every job's arguments
JOB_GAIN = float(os.environ.get("BR_JOB_GAIN", "0.5"))  # run a job subset when it costs under this share
RUN_MAX = float(os.environ.get("BR_RUN_MAX", "5.0"))  # run candidates up to this many seconds
PARALLEL = int(os.environ.get("BR_PARALLEL", "2"))
PROXY = os.environ.get("BR_PROXY", "1") == "1"
PROXY_BUDGET = float(os.environ.get("BR_PROXY_BUDGET", "1.5"))
PROXY_ALL = int(os.environ.get("BR_PROXY_ALL", "2"))  # 1: code edits, 2: also re-import cases
PROXY_MIN = int(os.environ.get("BR_PROXY_MIN", "3"))
PROXY_CHEAP = float(os.environ.get("BR_PROXY_CHEAP", "0.25"))  # CPU s: cheaper candidates always run
FIXTURE_FILTER = os.environ.get("BR_FIXTURE_FILTER", "1") == "1"
# code + data commits: plan the code part on its own. Off by default: on the practice board it
# ran more tests (CPU +8 s) and changed no selection.
MULTI_SPLIT = os.environ.get("BR_MULTI_SPLIT", "0") == "1"
NEW_MODULE = os.environ.get("BR_NEW_MODULE", "1") == "1"  # new modules: only importers and directory listers
FILTER_CAP = float(os.environ.get("BR_FILTER_CAP", "4"))  # per-test seconds when fixtures are filtered
WORKER_MS = 2000
DEBUG = os.environ.get("BR_DEBUG") == "1"

T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:6.2f}]", *a, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------
# patch parsing / applying
# ----------------------------------------------------------------------------

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _path(p):
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p[:2] in ("a/", "b/"):
        p = p[2:]
    return p


def parse_patch(text):
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.match(r"diff --git (\S+) (\S+)$", line)
            cur = {"old": None, "new": None, "hunks": [], "binary": False, "new_file": False, "deleted": False}
            if m:
                cur["old"] = _path(m.group(1))
                cur["new"] = _path(m.group(2))
            files.append(cur)
        elif cur is not None and line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            o = _path(line[4:])
            nw = _path(lines[i + 1][4:])
            cur["old"] = o
            cur["new"] = nw
            if o is None:
                cur["new_file"] = True
            if nw is None:
                cur["deleted"] = True
            i += 1
        elif cur is not None and line.startswith("new file mode"):
            cur["new_file"] = True
        elif cur is not None and line.startswith("deleted file mode"):
            cur["deleted"] = True
        elif cur is not None and line.startswith("rename from "):
            cur["old"] = line[len("rename from "):]
        elif cur is not None and line.startswith("rename to "):
            cur["new"] = line[len("rename to "):]
        elif cur is not None and (line.startswith("Binary files") or line.startswith("GIT binary patch")):
            cur["binary"] = True
        elif cur is not None and line.startswith("@@"):
            m = HUNK_RE.match(line)
            if m:
                os_ = int(m.group(1))
                ol = int(m.group(2)) if m.group(2) is not None else 1
                ns = int(m.group(3))
                nl = int(m.group(4)) if m.group(4) is not None else 1
                body = []
                i += 1
                o = c = 0
                while i < n and (o < ol or c < nl):
                    b = lines[i]
                    if b.startswith("\\"):
                        i += 1
                        continue
                    tag = b[:1]
                    if tag == "-":
                        o += 1
                    elif tag == "+":
                        c += 1
                    else:
                        o += 1
                        c += 1
                    body.append(b)
                    i += 1
                while i < n and lines[i].startswith("\\"):
                    i += 1
                cur["hunks"].append((os_, ol, ns, nl, body))
                continue
        i += 1
    return files


def apply_hunks(old_text, hunks):
    old = old_text.split("\n") if old_text is not None else []
    out = []
    pos = 0  # index into old
    ok = True
    for os_, ol, ns, nl, body in hunks:
        start = os_ - 1 if ol > 0 else os_
        if start < pos:
            ok = False
            start = pos
        out.extend(old[pos:start])
        j = start
        for b in body:
            tag = b[:1]
            content = b[1:]
            if tag == "+":
                out.append(content)
            elif tag == "-":
                if j >= len(old) or old[j] != content:
                    ok = False
                j += 1
            else:
                if j >= len(old) or old[j] != content:
                    ok = False
                out.append(old[j] if j < len(old) else content)
                j += 1
        pos = j
    out.extend(old[pos:])
    return "\n".join(out), ok


# ----------------------------------------------------------------------------
# change analysis
# ----------------------------------------------------------------------------


def _strip_bodies(tree):
    """Copy of tree with every outermost function body replaced by `pass`."""
    tree = copy.deepcopy(tree)

    class T(ast.NodeTransformer):
        def visit_FunctionDef(self, node):
            node.body = [ast.Pass()]
            return node

        visit_AsyncFunctionDef = visit_FunctionDef

    return T().visit(tree)


def _outer_defs(tree):
    """qualname -> [FunctionDef] for functions not nested in another function."""
    out = {}

    def walk(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.setdefault(prefix + ch.name, []).append(ch)
            elif isinstance(ch, ast.ClassDef):
                walk(ch, prefix + ch.name + ".")
            elif isinstance(ch, ast.stmt):
                walk(ch, prefix)

    walk(tree, "")
    return out


def _stmt_lists(tree):
    """(scope, [stmts]) for the module and each class body (function bodies stripped)."""
    res = []

    def collect(stmts, scope, flat):
        # statements inside if/try/with blocks belong to the same scope: flatten them, and keep
        # each block's header (its test, handlers' types...) as a statement of its own
        for s in stmts:
            if isinstance(s, (ast.If, ast.Try, ast.With)):
                head = copy.copy(s)
                for field in ("body", "orelse", "finalbody"):
                    if hasattr(head, field):
                        setattr(head, field, [])
                if isinstance(s, ast.Try):
                    head.handlers = [ast.ExceptHandler(type=h.type, name=h.name, body=[]) for h in s.handlers]
                flat.append(head)
                for field in ("body", "orelse", "finalbody"):
                    sub = getattr(s, field, None)
                    if sub:
                        collect(sub, scope, flat)
                if isinstance(s, ast.Try):
                    for h in s.handlers:
                        collect(h.body, scope, flat)
            else:
                flat.append(s)
                if isinstance(s, ast.ClassDef):
                    walk(s.body, scope + s.name + ".")

    def walk(stmts, scope):
        flat = []
        res.append((scope, flat))
        collect(stmts, scope, flat)

    walk(tree.body, "")
    return res


def _names_in(node):
    names = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            names.add(n.id)
        elif isinstance(n, ast.Attribute):
            names.add(n.attr)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.alias):
            names.add((n.asname or n.name).split(".")[0])
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
            names.add(n.value)
    return names


def _targets(node):
    t = set()
    if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
        tgts = node.targets if isinstance(node, ast.Assign) else [node.target]
        for tg in tgts:
            for n in ast.walk(tg):
                if isinstance(n, ast.Name):
                    t.add(n.id)
                elif isinstance(n, ast.Attribute):
                    t.add(n.attr)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        t.add(node.name)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            t.add((a.asname or a.name).split(".")[0])
    return t


def _strip_docstrings(tree):
    """Copy of tree with every module/class/function docstring removed."""
    import copy

    t = copy.deepcopy(tree)
    for node in ast.walk(t):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = node.body
            if b and isinstance(b[0], ast.Expr) and isinstance(getattr(b[0], "value", None), ast.Constant) and isinstance(b[0].value.value, str):
                node.body = b[1:]
    return t


def analyse_py(old_src, new_src):
    """Returns dict: kind in {'same','body','struct','syntax'}, funcs, names, lambda_lines."""
    try:
        old_tree = ast.parse(old_src) if old_src is not None else ast.parse("")
    except SyntaxError:
        return {"kind": "struct", "funcs": set(), "names": set(), "lambda_lines": set()}
    try:
        new_tree = ast.parse(new_src) if new_src is not None else ast.parse("")
    except SyntaxError:
        return {"kind": "syntax", "funcs": set(), "names": set(), "lambda_lines": set()}

    if ast.dump(old_tree) == ast.dump(new_tree):
        return {"kind": "same", "funcs": set(), "names": set(), "lambda_lines": set()}
    if ast.dump(_strip_docstrings(old_tree)) == ast.dump(_strip_docstrings(new_tree)):
        return {"kind": "doc", "funcs": set(), "names": set(), "lambda_lines": set()}

    od = _outer_defs(old_tree)
    nd = _outer_defs(new_tree)
    changed = set()
    for q in set(od) | set(nd):
        a = [ast.dump(x) for x in od.get(q, [])]
        b = [ast.dump(x) for x in nd.get(q, [])]
        if a != b:
            changed.add(q)

    so = _strip_bodies(old_tree)
    sn = _strip_bodies(new_tree)
    if ast.dump(so) == ast.dump(sn):
        # only function bodies differ; all changed qualnames exist once on both sides
        surgical = all(len(od.get(q, [])) == 1 and len(nd.get(q, [])) == 1 for q in changed)
        return {"kind": "body", "funcs": changed, "names": set(), "lambda_lines": set(), "surgical": surgical}

    # structural: find differing statements per scope, collect names and old lambda lines
    names = set()
    lambda_lines = set()
    scopes = set()
    import difflib

    old_scopes = dict(_stmt_lists(so))
    new_scopes = dict(_stmt_lists(sn))
    old_full = dict(_stmt_lists(old_tree))
    attr_changes = []
    classified = True
    for scope in set(old_scopes) | set(new_scopes):
        a = old_scopes.get(scope, [])
        b = new_scopes.get(scope, [])
        da = [ast.dump(s) for s in a]
        db = [ast.dump(s) for s in b]
        if da == db:
            continue
        sm = difflib.SequenceMatcher(None, da, db, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            olds = a[i1:i2]
            news = b[j1:j2]
            pairs = list(zip(olds, news)) if tag == "replace" and len(olds) == len(news) else None
            if tag in ("insert", "delete"):
                for st in olds + news:
                    ch = _add_change(scope.rstrip("."), st)
                    if ch is None:
                        classified = False
                    else:
                        attr_changes.append(ch)
            elif pairs is None:
                classified = False
            else:
                inner_only = True
                for o, n in pairs:
                    if isinstance(o, ast.ClassDef) and isinstance(n, ast.ClassDef) and o.name == n.name and _class_header(o) == _class_header(n):
                        continue  # only its body differs: handled in the class's own scope
                    inner_only = False
                    ch = _attr_change(scope.rstrip("."), o, n)
                    if ch is None:
                        classified = False
                    else:
                        attr_changes.append(ch)
                if inner_only:
                    continue
            for s in olds + news:
                names |= _targets(s)
                if scope:
                    scopes.add(scope.split(".")[0])
                elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    scopes.add(s.name)
                else:
                    scopes.add("")
            on = set().union(*[_names_in(s) for s in olds]) if olds else set()
            nn = set().union(*[_names_in(s) for s in news]) if news else set()
            names |= on ^ nn
            full_old = old_full.get(scope, [])
            for s in full_old[i1:i2] if len(full_old) == len(a) else []:
                for n in ast.walk(s):
                    if isinstance(n, ast.Lambda):
                        lambda_lines.add(n.lineno)
    return {
        "kind": "struct",
        "funcs": changed,
        "names": names,
        "lambda_lines": lambda_lines,
        "scopes": scopes,
        "attr_changes": attr_changes if classified else None,
    }


def _add_change(scope, s):
    """A definition added or removed whole: the names it binds. None if it may have side effects."""
    if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        if s.decorator_list or (isinstance(s, ast.ClassDef) and (s.bases or s.keywords)):
            return None  # decorators / subclass hooks run at definition time
        names = {s.name}
    elif isinstance(s, ast.Assign) and all(isinstance(t, ast.Name) for t in s.targets):
        if any(isinstance(n, ast.Call) for n in ast.walk(s.value)):
            return None
        names = {t.id for t in s.targets}
    elif isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name):
        names = {s.target.id}
    elif isinstance(s, (ast.Import, ast.ImportFrom)):
        mod = getattr(s, "module", None) or ""
        if getattr(s, "level", 0) or mod.startswith("sqlglot") or any(a.name.startswith("sqlglot") for a in s.names):
            return None  # importing package modules can register dialects etc.
        names = {(a.asname or a.name).split(".")[0] for a in s.names}
    else:
        return None
    return {"kind": "add", "scope": scope, "targets": sorted(names)}


def _class_header(c):
    return ast.dump(ast.ClassDef(name=c.name, bases=c.bases, keywords=c.keywords, body=[], decorator_list=c.decorator_list, type_params=[]))


def _assign_parts(s):
    if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name):
        return s.targets[0].id, s.value, None
    if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name) and s.value is not None:
        return s.target.id, s.value, ast.dump(s.annotation)
    return None


def _attr_change(scope, o, n):
    """Classify a changed class/module attribute assignment, or None."""
    po, pn = _assign_parts(o), _assign_parts(n)
    if po is None or pn is None or po[0] != pn[0] or po[2] != pn[2]:
        return None
    name, ov, nv = po[0], po[1], pn[1]
    if isinstance(ov, ast.Dict) and isinstance(nv, ast.Dict):
        oe = {ast.dump(k) if k is not None else "**" + ast.dump(v): (k, v) for k, v in zip(ov.keys, ov.values)}
        ne = {ast.dump(k) if k is not None else "**" + ast.dump(v): (k, v) for k, v in zip(nv.keys, nv.values)}
        keys, lambda_lines, classkeys, valstrs = set(), set(), set(), set()
        for kd in set(oe) | set(ne):
            a, b = oe.get(kd), ne.get(kd)
            if a is not None and b is not None and ast.dump(a[1]) == ast.dump(b[1]):
                continue
            for ent in (a, b):
                if ent is None:
                    continue
                k = ent[0]
                if isinstance(k, ast.Constant) and isinstance(k.value, str) and k.value.strip():
                    keys.add(k.value)
                elif isinstance(k, ast.Attribute) and isinstance(k.value, ast.Name) and k.value.id == "exp":
                    classkeys.add(k.attr)
                else:
                    return None  # spreads or computed keys: no handle
                for x in ast.walk(ent[1]):
                    if isinstance(x, ast.Constant) and isinstance(x.value, str) and len(x.value.strip()) >= 3:
                        valstrs.add(x.value.strip())
            if a is not None:
                for x in ast.walk(a[1]):
                    if isinstance(x, ast.Lambda):
                        lambda_lines.add(x.lineno)
        if classkeys and not valstrs and not lambda_lines:
            pass  # value is a plain reference: class-key readers decide
        return {
            "kind": "dict",
            "scope": scope,
            "target": name,
            "keys": sorted(keys),
            "classkeys": sorted(classkeys),
            "valstrs": sorted(valstrs - keys),
            "lambda_lines": sorted(lambda_lines),
        }
    if isinstance(ov, (ast.Dict, ast.Lambda)) or isinstance(nv, (ast.Dict, ast.Lambda)):
        return None
    return {"kind": "scalar", "scope": scope, "target": name, "value": ast.unparse(nv)}


def _stmt_ranges(tree):
    """[(lo, hi)] own line range per statement: compound statements own only their header."""
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.stmt):
            continue
        lo, hi = n.lineno, getattr(n, "end_lineno", n.lineno) or n.lineno
        body = getattr(n, "body", None)
        full = (lo, hi)
        if isinstance(body, list) and body and isinstance(body[0], ast.AST):
            first = min([body[0].lineno] + [d.lineno for d in getattr(n, "decorator_list", [])] or [body[0].lineno])
            hdr_hi = max(lo, body[0].lineno - 1)
            out.append((lo, hdr_hi, full))
        else:
            out.append((lo, hi, full))
    return out


def probe_lines(old_src, hunks):
    """Old-file lines whose execution is a precondition for the change to matter."""
    try:
        tree = ast.parse(old_src)
    except SyntaxError:
        return None
    changed, inserts = set(), set()
    for os_, ol, ns, nl, body in hunks:
        j = os_ if ol > 0 else os_ + 1
        prev = " "
        for b in body:
            tag = b[:1]
            if tag == "-":
                changed.add(j)
                j += 1
            elif tag == "+":
                if prev != "-" and prev != "r":
                    inserts.add(j)  # pure insertion: no removed line it replaces
                else:
                    tag = "r"  # replacement of the removed lines just above
            else:
                j += 1
            prev = tag
    ranges = _stmt_ranges(tree)

    def own(line):
        best = None
        for lo, hi, full in ranges:
            if full[0] <= line <= full[1]:
                if best is None or (full[1] - full[0]) < (best[2][1] - best[2][0]):
                    best = (lo, hi, full)
        if best is None:
            return None
        return best[0], best[1]

    probe = set()
    for line in changed:
        r = own(line)
        if r is None:
            return None  # change outside any statement: cannot localise
        probe.update(range(r[0], r[1] + 1))
    for line in inserts:
        for ln in (line - 1, line):
            r = own(ln)
            if r is not None:
                probe.update(range(r[0], r[1] + 1))
            # code added after a loop/if body runs whenever its enclosing headers do
            for lo, hi, full in ranges:
                if full[0] <= ln <= full[1]:
                    probe.update(range(lo, hi + 1))
    return probe


_LINES = {}


def line_tests(f, lines, tests):
    """Tests (numbers) that executed any of these lines of f at the pinned commit; None if unknown."""
    if not LINE_FILTER:
        return None
    if "raw" not in _LINES:
        try:
            with gzip.open(LINES_PATH, "rt") as fh:
                raw = json.load(fh)
            num_by_id = {t["id"]: t["number"] for t in tests}
            _LINES["remap"] = [num_by_id.get(i) for i in raw["ids"]]
            _LINES["raw"] = raw["files"]
            _LINES["jobs"] = {num_by_id[t]: v for t, v in raw.get("jobs", {}).items() if t in num_by_id}
        except Exception as e:
            log("line data unavailable", repr(e))
            _LINES["raw"] = None
    raw = _LINES["raw"]
    if raw is None:
        return None
    if f not in _LINES:
        s = raw.get(f)
        if s is None:
            _LINES[f] = None
        else:
            d = json.loads(s)
            remap = _LINES["remap"]
            sets = [frozenset(remap[i] for i in st if remap[i] is not None) for st in d["s"]]
            _LINES[f] = {ln: sets[k] for ln, k in d["l"]}
    byline = _LINES[f]
    if byline is None:
        return set()  # no test executed any line of f
    out = set()
    for ln in lines:
        ts = byline.get(ln)
        if ts:
            out |= ts
    return out


_JOBS = {}


def job_data(n, tests):
    """Per-job line coverage of pool test n (each submit() is one job), or None."""
    if "raw" not in _LINES:
        line_tests("", (), tests)  # loads the file
    raw = (_LINES.get("jobs") or {}).get(n)
    if raw is None:
        return None
    if n not in _JOBS:
        d = json.loads(raw)
        sets = [frozenset(x) for x in d["s"]]
        _JOBS[n] = {
            "n": d["n"],
            "none": set(d["none"]),
            "t": d["t"],
            "main": {f: set(v) for f, v in d["m"].items()},
            "by": {f: {ln: sets[k] for ln, k in v} for f, v in d["l"].items()},
        }
    return _JOBS[n]


def job_subset(n, an, tests):
    """Jobs of pool test n that run a changed line; None when every job may be affected."""
    jd = job_data(n, tests)
    if jd is None or not jd["none"]:
        return None  # a skipped job must look like one that returned None
    J = set()
    for f, lines in an.job_probe.items():
        if not lines.isdisjoint(jd["main"].get(f, ())):
            return None
        by = jd["by"].get(f)
        if by:
            for ln in lines:
                js = by.get(ln)
                if js:
                    J |= js
    return J


class Analysis:
    def __init__(self, cid, files, tests, mp):
        self.cid = cid
        self.files = files
        self.tests = tests
        self.mp = mp
        self.cands = set()
        self.patched = {}  # abs path -> new text (None = deleted)
        self.surgery = {}  # module rel path -> set(qualnames)
        self.full_reload = False
        self.reasons = []
        self.unknown = False
        self.proxy_ok = True
        self.stop_on_fail = False
        self.attr_surgery = []
        self.proxy_heavy = set()
        self.job_probe = {}  # file -> probed lines, when every change is a localised body edit
        self.job_clean = 0
        self.job_files = 0
        self.job_filter = {}  # test -> jobs to run
        self.deferred = []  # costly tests run (on a job subset) only once a cheap test fails
        self.scaled = set()  # deferred tests that run at a reduced size
        self.standin = set()  # costly tests a TPC-DS stand-in may clear after a cheap failure
        self.mode = "code"  # what the file being analysed is: "code" (.py) or "data"
        self.code_cands = set()  # candidates because of python edits
        self.data_tests = set()  # candidates because of data/fixture edits
        self.proxy_ok_code = True  # stand-ins allowed as far as the python edits go
        self.pre = []  # data readers run first; their failures say nothing about the code edit
        self.evidence = []  # data readers the code edit also reaches: rerun on the old data files

    def add(self, ts, why):
        before = len(self.cands)
        ts = set(ts)
        self.cands |= ts
        (self.data_tests if self.mode == "data" else self.code_cands).update(ts)
        if DEBUG:
            self.reasons.append(f"{why}:+{len(self.cands) - before}")


def tests_of_unit_prefix(mp, f, qual):
    """tests entering function `qual` in file f, incl. nested code (qual.<locals>...)."""
    out = set()
    ub = mp["unit_index"].get(f, {})
    for q, ts in ub.items():
        if q == qual or q.startswith(qual + ".<locals>.") or q.startswith(qual + "::"):
            out |= ts
    return out


def tests_of_scope(mp, f, T):
    out = set()
    for q, ts in mp["unit_index"].get(f, {}).items():
        if q == T or q.startswith(T + "."):
            out |= ts
    return out


HOT = int(os.environ.get("BR_HOT", "150"))
FAMILY_DIRS = ("dialects", "parsers", "generators", "tokenizers", "typing")


def dialect_family(mp, f):
    """Files of the same dialect in sibling packages, plus dialects built on top of it."""
    parts = f.split("/")
    if len(parts) != 3 or parts[1] not in FAMILY_DIRS or parts[2] == "__init__.py":
        return set()
    base = parts[2]
    fam = set(mp["by_basename"].get(base, ())) | {f}
    frontier = list(fam)
    seen = set(fam)
    while frontier:
        g = frontier.pop()
        for imp in mp["file_importers"].get(g, ()):
            p = imp.split("/")
            if imp in seen or len(p) != 3 or p[1] not in FAMILY_DIRS or p[2] == "__init__.py":
                continue
            seen.add(imp)
            frontier.append(imp)
            for sib in mp["by_basename"].get(p[2], ()):
                if sib not in seen:
                    seen.add(sib)
                    frontier.append(sib)
    return seen


_TEST_AST = {}
_FIXTURE_TEXT = None


def tests_mentioning(mp, tests, keys):
    """Tests whose own source (or a fixture they read) mentions any key (case-insensitive)."""
    global _FIXTURE_TEXT
    pats = [re.compile(r"\s+".join(re.escape(w) for w in k.split()), re.I) for k in keys if k.strip()]
    out = set()
    if not pats:
        return out
    for tf, src in mp["test_sources"].items():
        if not any(p.search(src) for p in pats):
            continue
        tree = _TEST_AST.get(tf)
        if tree is None:
            tree = _TEST_AST[tf] = ast.parse(src)
        lines = src.split("\n")
        covered = []
        for cls in tree.body:
            if not isinstance(cls, ast.ClassDef):
                continue
            for fn in cls.body:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    seg = "\n".join(lines[fn.lineno - 1 : fn.end_lineno])
                    covered.append((fn.lineno, fn.end_lineno))
                    if any(p.search(seg) for p in pats):
                        tid = f"{tf}::{cls.name}::{fn.name}"
                        for i in mp["tests_by_file"].get(tf, ()):
                            if tests[i]["id"] == tid:
                                out.add(i)
        # mentioned outside test methods (helpers, module constants): the whole file
        mask = set()
        for lo, hi in covered:
            mask.update(range(lo, hi + 1))
        rest = "\n".join(l for i, l in enumerate(lines, 1) if i not in mask)
        if any(p.search(rest) for p in pats):
            out.update(mp["tests_by_file"].get(tf, ()))
    if _FIXTURE_TEXT is None:
        _FIXTURE_TEXT = {}
        for rel in mp["opens"]:
            if rel.startswith("tests/") and not rel.endswith((".py", ".pyc")) and "__pycache__" not in rel:
                try:
                    with open(os.path.join(REPO, rel), encoding="utf-8", errors="replace") as fh:
                        _FIXTURE_TEXT[rel] = fh.read()
                except OSError:
                    pass
    for rel, txt in _FIXTURE_TEXT.items():
        if any(p.search(txt) for p in pats):
            out |= mp["opens"].get(rel, set())
    return out


_CLASS_QUALS = {}


def _class_body_quals(mp, f):
    """qualnames of class bodies in file f (code units whose qualname is a class)."""
    q = _CLASS_QUALS.get(f)
    if q is None:
        try:
            tree = ast.parse(open(os.path.join(REPO, f), encoding="utf-8").read())
        except (OSError, SyntaxError):
            tree = ast.parse("")
        q = set()

        def walk(node, prefix):
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, ast.ClassDef):
                    q.add(prefix + ch.name)
                    walk(ch, prefix + ch.name + ".")
                elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(ch, prefix + ch.name + ".<locals>.")
                else:
                    walk(ch, prefix)

        walk(tree, "")
        _CLASS_QUALS[f] = q
    return q


def precise_attr_changes(an, mp, tests, f, changes, res):
    """Candidates for edited flags/constants and string-keyed dict entries. False -> fall back."""
    fam = dialect_family(mp, f)
    fam_tests = set()
    for g in fam:
        fam_tests |= mp["files"].get(g, set())
    cands = set()
    surgery = []
    reload_needed = False
    for ch in changes:
        if ch["kind"] == "scalar":
            readers = [u for u in mp["names"].get(ch["target"], ()) if u != f + "::" + ch["scope"]]
            for u in readers:
                uf, uq = u.split("::", 1)
                if uq == "<module>" or (uf.startswith("sqlglot/") and uq in _class_body_quals(mp, uf)):
                    return False  # read by class/module bodies: derived values elsewhere
                if u in mp["lib_import_units"]:
                    reload_needed = True  # a function also run while importing: re-import
                if mp["collect_units"].get(u):  # read while importing a test module: re-import
                    reload_needed = True
            line_files = set()
            for k, nm in mp["flag_lines"].items():
                if ch["target"] in nm:
                    kf, _, kl = k.rpartition(":")
                    line_files.add(kf)
                    ts = set(mp["flag_hits"].get(k, ()))
                    for t, hl in mp["heavy_lines"].items():
                        if int(kl) in hl.get(kf, ()):
                            ts.add(t)
                    if fam and len(ts) > HOT and kf not in fam:
                        ts &= fam_tests
                    cands |= ts
            for u in readers:
                uf, uq = u.split("::", 1)
                if uf in line_files:
                    continue  # the exact reading lines decided it
                ts = tests_of_unit_prefix(mp, uf, uq)
                if fam and len(ts) > HOT and uf not in fam:
                    ts = ts & fam_tests
                cands |= ts
            if ch["scope"]:
                surgery.append((f, ch["scope"], ch["target"], ch["value"]))
            else:
                reload_needed = True
        elif ch["kind"] == "add":
            # a name defined or removed whole: only code reading that name (or tests naming it) can notice
            reload_needed = True
            for nm in ch["targets"]:
                for u in mp["names"].get(nm, ()):
                    uf, uq = u.split("::", 1)
                    if uq == "<module>" or (uf.startswith("sqlglot/") and uq in _class_body_quals(mp, uf)):
                        return False
                    ts = tests_of_unit_prefix(mp, uf, uq)
                    if fam and len(ts) > HOT and uf not in fam:
                        ts = ts & fam_tests
                    cands |= ts
                ment = tests_mentioning(mp, tests, [nm]) if len(nm) > 2 else set()
                if fam and len(ment) > HOT:
                    ment &= fam_tests
                cands |= ment
        else:  # dict entries keyed by SQL text or expression class
            reload_needed = True
            for cls_name in ch.get("classkeys", ()):
                ts = set()
                for u in mp["names"].get(cls_name, ()):
                    uf, uq = u.split("::", 1)
                    if uq == "<module>" or u in mp["lib_import_units"]:
                        continue
                    ts |= tests_of_unit_prefix(mp, uf, uq)
                for uf, ub in mp["unit_index"].items():
                    for q, t2 in ub.items():
                        if q == cls_name or q.startswith(cls_name + "."):
                            ts |= t2
                if fam:
                    ts &= fam_tests
                cands |= ts
            if ch.get("valstrs"):
                vm = tests_mentioning(mp, tests, ch["valstrs"])
                if fam and len(vm) > HOT:
                    vm &= fam_tests
                cands |= vm
            for ln in ch["lambda_lines"]:
                for q, ts in mp["unit_index"].get(f, {}).items():
                    if q.endswith("<lambda>@%d" % ln):
                        cands |= ts
            ment = tests_mentioning(mp, tests, ch["keys"])
            if fam and len(ment) > HOT:
                ment = ment & fam_tests
            cands |= ment
    an.add(cands, "attr:" + ",".join(c.get("target") or "+".join(c.get("targets", ())) for c in changes))
    if f.startswith("sqlglot/"):
        an.add(mp["spawn_tests"], "spawn")
    if reload_needed:
        an.full_reload = True
    else:
        an.attr_surgery.extend(surgery)
    return True


_DISCOVERY_WORDS = ("pkgutil", "listdir", "iter_modules", "walk_packages", "scandir", "iterdir", "glob(")


def _shadows(rel):
    """Would a new module at rel take the place of one that exists (module vs package)?"""
    base = os.path.join(REPO, rel)
    if os.path.exists(base):
        return True
    if rel.endswith("/__init__.py"):
        return os.path.exists(os.path.dirname(base) + ".py")
    return os.path.isdir(base[:-3])


_SOURCES = []


def _named_anywhere(rel):
    """Does existing code name this module, so it may import it once it exists
    (an optional `try: import`, or a lazy-load list of module names)?"""
    if not _SOURCES:
        for top in ("sqlglot", "tests"):
            for d, _, fs in os.walk(os.path.join(REPO, top)):
                if "__pycache__" in d or "fixtures" in d:
                    continue
                for fn in fs:
                    if fn.endswith(".py"):
                        try:
                            with open(os.path.join(d, fn), encoding="utf-8", errors="replace") as fh:
                                _SOURCES.append(fh.read())
                        except OSError:
                            pass
        _SOURCES.append("")
    mod = rel[:-3]
    if mod.endswith("/__init__"):
        mod = mod[: -len("/__init__")]
    short = mod.rsplit("/", 1)[-1]
    w = r"(?<![\w])" + re.escape(short) + r"(?![\w])"
    pat = re.compile(
        r"(?:^|[\s.;])(?:import|from)\s+[\w.]*" + w
        + r"|(?:^|[\s;])import\s+[\w., ]*" + w
        + r"|['" + '"' + r"]" + re.escape(short) + r"['" + '"' + r"]"
        + r"|['" + '"' + r"][\w.]*\." + re.escape(short) + r"['" + '"' + r"]",
        re.M,
    )
    return any(pat.search(src) for src in _SOURCES)


def analyse_commit(cid, text, mp, tests):
    an = Analysis(cid, parse_patch(text), tests, mp)
    id_by_file = mp["tests_by_file"]
    for fd in an.files:
        old_rel = fd["old"]
        new_rel = fd["new"]
        rel = new_rel or old_rel
        if rel is None:
            continue
        an.job_files += 1
        an.mode = "code" if rel.endswith(".py") else "data"
        if fd["binary"]:
            an.add(mp["opens"].get(rel, ()), "binary-open")
            an.add(mp["files"].get(rel, ()), "binary-file")
            an.unknown = True
            continue
        old_abs = os.path.join(REPO, old_rel) if old_rel else None
        old_text = None
        if old_abs and not fd["new_file"]:
            try:
                with open(old_abs, encoding="utf-8", newline="") as f:
                    old_text = f.read()
            except OSError:
                old_text = None
        if fd["deleted"]:
            new_text = None
        else:
            new_text, ok = apply_hunks(old_text if old_text is not None else "", fd["hunks"])
            if not ok:
                log(cid, "hunk mismatch", rel)
        if old_rel and new_rel and old_rel != new_rel:
            an.patched[os.path.join(REPO, old_rel)] = None
        if new_rel:
            an.patched[os.path.join(REPO, new_rel)] = new_text
        elif old_rel:
            an.patched[os.path.join(REPO, old_rel)] = None

        for r in {old_rel, new_rel} - {None}:
            if r.startswith("tests/") and r.endswith(".py"):
                continue  # test modules are "opened" by their own collection; the AST diff below decides
            an.add(mp["opens"].get(r, ()), "opened")

        if rel.endswith(".py") and rel.startswith("tests/"):
            an.proxy_ok = False
            an.proxy_ok_code = False
        if rel.endswith(".py") and (rel.startswith("sqlglot/") or rel.startswith("tests/")):
            res = analyse_py(old_text, new_text if not fd["deleted"] else None)
            kind = res["kind"]
            if kind == "same" and old_rel == new_rel:
                an.job_clean += 1
                continue
            if (
                NEW_MODULE
                and fd["new_file"]
                and not fd["deleted"]
                and kind != "syntax"
                and rel.startswith("sqlglot/")
                and old_rel in (None, new_rel)
                and not _shadows(rel)
                and not _named_anywhere(rel)
            ):
                # a brand-new module runs only if something imports it: a changed file
                # (analysed on its own) or a test that lists a package directory
                for tf, src in mp["test_sources"].items():
                    if any(k in src for k in _DISCOVERY_WORDS):
                        an.add(id_by_file.get(tf, ()), "new-module-discovery")
                an.full_reload = True
                continue
            if kind == "doc" and old_rel == new_rel and rel.startswith("sqlglot/"):
                # only docstrings changed: they matter to doctests and to code reading __doc__
                for tf in mp["test_sources"]:
                    if "doctest" in mp["test_sources"][tf]:
                        an.add(id_by_file.get(tf, ()), "doc-doctest")
                an.add(tests_mentioning(mp, tests, ["__doc__"]), "doc-ref")
                an.full_reload = True
                continue
            f = old_rel or new_rel
            if kind == "body" and old_rel == new_rel:
                probe = probe_lines(old_text, fd["hunks"])
                heavy_lines = mp["heavy_lines"]
                for q in res["funcs"]:
                    ts = tests_of_unit_prefix(mp, f, q)
                    at_import = (f + "::" + q) in mp["import_units"] or any(
                        u.startswith(f + "::" + q + ".") for u in mp["import_by_file"].get(f, ())
                    )
                    if probe is not None and not at_import and f.startswith("sqlglot/"):
                        hit = line_tests(f, probe, tests)
                        if hit is not None:
                            keep = (ts & hit) | (ts & mp["spawn_tests"])
                            if DEBUG and len(keep) != len(ts):
                                an.reasons.append(f"lines:-{len(ts) - len(keep)}")
                            ts = keep
                    if probe is not None:
                        keep = set()
                        for t in ts:
                            hl = heavy_lines.get(t)
                            if hl is None or any(
                                u == f + "::" + q or u.startswith(f + "::" + q + ".") for u in mp["heavy_setup"].get(t, ())
                            ):
                                keep.add(t)
                            elif not probe.isdisjoint(hl.get(f, ())):
                                keep.add(t)
                        if DEBUG and len(keep) != len(ts):
                            an.reasons.append(f"linefilter:-{len(ts) - len(keep)}")
                        ts = keep
                    an.add(ts, f"body:{q}")
                    # a changed test method selects itself
                    tid_prefix = f + "::" + q.replace(".", "::")
                    for i in id_by_file.get(f, ()):
                        if tests[i]["id"] == tid_prefix or tests[i]["id"].startswith(tid_prefix + "["):
                            an.add([i], "self")
                if res.get("surgical", False):
                    an.surgery.setdefault(f, set()).update(res["funcs"])
                else:
                    an.full_reload = True
                if (
                    probe is not None
                    and res.get("surgical", False)
                    and f.startswith("sqlglot/")
                    and not any(
                        (f + "::" + q) in mp["import_units"]
                        or any(u.startswith(f + "::" + q + ".") for u in mp["import_by_file"].get(f, ()))
                        for q in res["funcs"]
                    )
                ):
                    an.job_clean += 1
                    an.job_probe.setdefault(f, set()).update(probe)
                # executed at import time? surgery can't replay that
                for q in res["funcs"]:
                    if (f + "::" + q) in mp["import_units"] or any(
                        u.startswith(f + "::" + q + ".") for u in mp["import_by_file"].get(f, ())
                    ):
                        an.full_reload = True
                continue
            # class/module attribute edits we can localise precisely
            ac = res.get("attr_changes")
            if kind == "struct" and ac and old_rel == new_rel and not fd["new_file"] and not fd["deleted"]:
                if precise_attr_changes(an, mp, tests, f, ac, res):
                    for q in res["funcs"]:
                        an.add(tests_of_unit_prefix(mp, f, q), f"struct-func:{q}")
                    continue
            # structural, syntax error, add/delete/rename
            an.full_reload = True
            scopes = res.get("scopes")
            whole = (
                kind == "syntax"
                or fd["new_file"]
                or fd["deleted"]
                or old_rel != new_rel
                or not scopes
                or "" in scopes
            )
            for ff in {old_rel, new_rel} - {None}:
                if whole:
                    an.add(mp["files"].get(ff, ()), "struct-file")
                    an.add(id_by_file.get(ff, ()), "struct-testfile")
                else:
                    for T in scopes:
                        an.add(tests_of_scope(mp, ff, T), f"scope:{T}")
                        for i in id_by_file.get(ff, ()):
                            if tests[i]["id"].startswith(ff + "::" + T + "::"):
                                an.add([i], "scope-test")
                if ff.startswith("sqlglot/"):
                    for fam in dialect_family(mp, ff):
                        an.add(mp["files"].get(fam, ()), "family")
                    an.add(mp["spawn_tests"], "spawn")
            for q in res["funcs"]:
                an.add(tests_of_unit_prefix(mp, f, q), f"struct-func:{q}")
            cap = 60 if whole else 25
            specific = not whole and bool(dialect_family(mp, f))
            for nm in res["names"]:
                refs = mp["names"].get(nm, ())
                if len(refs) > cap:
                    continue
                for u in refs:
                    uf, uq = u.split("::", 1)
                    ts = tests_of_unit_prefix(mp, uf, uq)
                    # a dialect-specific value read by generic code every test runs:
                    # the dialect's own tests (family) already cover that path
                    if specific and len(ts) > HOT and not dialect_family(mp, uf):
                        continue
                    an.add(ts, f"name:{nm}")
            if kind == "syntax" or fd["new_file"] or fd["deleted"]:
                an.unknown = True
                # an import failure takes down everything importing the module
                mod = f[:-3].replace("/", ".")
                if mod.endswith(".__init__"):
                    mod = mod[: -len(".__init__")]
                an.add(mp["module_importers"].get(mod, ()), "importers")
            continue

        if rel.endswith(".py"):
            # python outside the package/tests (setup.py, docs, scripts)
            an.add(mp["files"].get(rel, ()), "other-py")
            continue

        # data / fixture files
        an.proxy_ok = False
        if rel.startswith("tests/") or rel.startswith("sqlglot/"):
            if not mp["opens"].get(rel) and not (old_rel and mp["opens"].get(old_rel)):
                # nobody read it at the pinned commit: tests whose source names it
                base = rel.rsplit("/", 1)[-1]
                stem = base.split(".")[0]
                for tf, src in mp["test_sources"].items():
                    if base in src or (len(stem) > 3 and stem in src):
                        an.add(id_by_file.get(tf, ()), "name-in-test")
    return an


# ----------------------------------------------------------------------------
# map loading
# ----------------------------------------------------------------------------


def load_map(tests):
    with gzip.open(MAP_PATH, "rt") as f:
        raw = json.load(f)
    ids = raw["ids"]
    num_by_id = {t["id"]: t["number"] for t in tests}
    remap = [num_by_id.get(i) for i in ids]

    def conv(lst):
        return {remap[i] for i in lst if remap[i] is not None}

    unit_index = {}
    for u, lst in raw["units"].items():
        f, q = u.split("::", 1)
        unit_index.setdefault(f, {})[q] = conv(lst)
    import_by_file = {}
    for u in raw["import_units"]:
        f, q = u.split("::", 1)
        import_by_file.setdefault(f, set()).add(u)
    tests_by_file = {}
    for t in tests:
        tests_by_file.setdefault(t["file"], []).append(t["number"])
    by_basename = {}
    for f in raw["files"]:
        if f.startswith("sqlglot/") and f.count("/") == 2:
            by_basename.setdefault(f.rsplit("/", 1)[-1], []).append(f)
    test_sources = {}
    for tf in tests_by_file:
        try:
            with open(os.path.join(REPO, tf), encoding="utf-8") as fh:
                test_sources[tf] = fh.read()
        except OSError:
            pass
    module_importers = {m: conv(lst) for m, lst in raw.get("module_importers", {}).items()}
    return {
        "unit_index": unit_index,
        "files": {f: conv(lst) for f, lst in raw["files"].items()},
        "opens": {p: conv(lst) for p, lst in raw["opens"].items()},
        "import_units": set(raw["import_units"]),
        "import_by_file": import_by_file,
        "names": raw["names"],
        "tests_by_file": tests_by_file,
        "by_basename": by_basename,
        "test_sources": test_sources,
        "module_importers": module_importers,
        "spawn_tests": conv(raw.get("spawn_tests", [])),
        "lib_import_units": set(raw.get("lib_import_units", [])),
        "flag_lines": raw.get("flag_lines", {}),
        "flag_hits": {k: conv(v) for k, v in raw.get("flag_hits", {}).items()},
        "collect_units": raw.get("collect_units", {}),
        "cpu": {remap[int(i)]: v for i, v in raw.get("cpu", {}).items() if remap[int(i)] is not None},
        "file_importers": raw.get("file_importers", {}),
        "heavy_lines": {
            remap[int(i)]: {f: set(v) for f, v in byf.items()}
            for i, byf in raw.get("heavy_lines", {}).items()
            if remap[int(i)] is not None
        },
        "heavy_setup": {
            remap[int(i)]: set(us) for i, us in raw.get("heavy_setup_units", {}).items() if remap[int(i)] is not None
        },
    }


# ----------------------------------------------------------------------------
# running tests in a forked child
# ----------------------------------------------------------------------------


class _PatchedLoader(importlib.abc.Loader):
    def __init__(self, path, src):
        self.path = path
        self.src = src

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        code = compile(self.src, self.path, "exec", dont_inherit=True)
        exec(code, module.__dict__)


class _PatchedFinder(importlib.abc.MetaPathFinder):
    def __init__(self, files):
        self.files = files  # abs path -> text or None

    def find_spec(self, name, path, target=None):
        if not (name == "sqlglot" or name.startswith("sqlglot.") or name == "tests" or name.startswith("tests.")):
            return None
        rel = name.replace(".", "/")
        for cand, pkg in ((os.path.join(REPO, rel + ".py"), False), (os.path.join(REPO, rel, "__init__.py"), True)):
            if cand in self.files:
                src = self.files[cand]
                if src is None:
                    raise ModuleNotFoundError(name)
                return importlib.util.spec_from_file_location(
                    name,
                    cand,
                    loader=_PatchedLoader(cand, src),
                    submodule_search_locations=[os.path.dirname(cand)] if pkg else None,
                )
        return None


_EVID = [False]  # child: results now are evidence only (reported as -1 - number)
_JOBF = {}  # test number -> jobs (submit indices) to run; the others return None
_CUR = [None]  # test number running in this child


class InlineExecutor:
    """Stand-in for the spawn ProcessPoolExecutor some tests use: spawned workers
    would import sqlglot from disk (unpatched) and cost a process start each."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            import pickle

            initializer(*pickle.loads(pickle.dumps(initargs)))
        self._k = 0
        self._only = _JOBF.get(_CUR[0])

    def submit(self, fn, *a, **kw):
        import concurrent.futures as cf

        import pickle

        fut = cf.Future()
        k = self._k
        self._k += 1
        if self._only is not None and k not in self._only:
            fut.set_result(None)  # no changed line runs in this job
            return fut
        try:
            # spawn workers receive pickled work and return pickled results
            fn, a, kw = pickle.loads(pickle.dumps((fn, a, kw)))
            fut.set_result(pickle.loads(pickle.dumps(fn(*a, **kw))))
        except BaseException as e:
            fut.set_exception(e)
        return fut

    def shutdown(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_pools():
    for name, m in list(sys.modules.items()):
        if name.startswith("tests") and getattr(m, "ProcessPoolExecutor", None) not in (None, InlineExecutor):
            m.ProcessPoolExecutor = InlineExecutor


_OVERLAY_SITE = r'''
import importlib.abc, importlib.util, json, os, sys
_d = os.path.dirname(os.path.abspath(__file__))
_files = json.load(open(os.path.join(_d, "patched.json"), encoding="utf-8"))
_repo = os.environ.get("BR_OVERLAY_REPO", "")
class _L(importlib.abc.Loader):
    def __init__(self, p, s): self.p, self.s = p, s
    def create_module(self, spec): return None
    def exec_module(self, m): exec(compile(self.s, self.p, "exec", dont_inherit=True), m.__dict__)
class _F(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if not (name == "sqlglot" or name.startswith(("sqlglot.", "tests"))): return None
        rel = name.replace(".", "/")
        for cand, pkg in ((os.path.join(_repo, rel + ".py"), False), (os.path.join(_repo, rel, "__init__.py"), True)):
            if cand in _files:
                s = _files[cand]
                if s is None: raise ModuleNotFoundError(name)
                return importlib.util.spec_from_file_location(name, cand, loader=_L(cand, s),
                    submodule_search_locations=[os.path.dirname(cand)] if pkg else None)
        return None
sys.meta_path.insert(0, _F())
'''


def _install_subprocess_overlay(pyfiles):
    """Child processes the tests start (python -c ...) must see the patched code too."""
    d = os.path.join(_child_tmp(), "overlay")
    os.makedirs(d, exist_ok=True)
    with io.open(os.path.join(d, "patched.json"), "w", encoding="utf-8") as fh:
        json.dump(pyfiles, fh)
    with io.open(os.path.join(d, "sitecustomize.py"), "w", encoding="utf-8") as fh:
        fh.write(_OVERLAY_SITE)
    os.environ["BR_OVERLAY_REPO"] = REPO
    os.environ["PYTHONPATH"] = d + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def _child_tmp():
    d = os.path.join(os.environ.get("TMPDIR", "/tmp"), "br%d" % os.getpid())
    os.makedirs(d, exist_ok=True)
    return d


class _Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise _Timeout("timeout")


def _module_name(rel):
    m = rel[:-3].replace("/", ".")
    return m[: -len(".__init__")] if m.endswith(".__init__") else m


def _code_objects(code):
    stack = [code]
    while stack:
        c = stack.pop()
        yield c
        for k in c.co_consts:
            if isinstance(k, types.CodeType):
                stack.append(k)


def _find_live_function(module, qual):
    obj = module
    parts = qual.split(".")
    for p in parts:
        d = getattr(obj, "__dict__", None)
        if d is None or p not in d:
            return None
        obj = d[p]
    seen = 0
    while obj is not None and seen < 10:
        seen += 1
        if isinstance(obj, types.FunctionType):
            if obj.__code__.co_qualname == qual:
                return obj
            obj = getattr(obj, "__wrapped__", None)
        elif isinstance(obj, (staticmethod, classmethod)):
            obj = obj.__func__
        elif isinstance(obj, property):
            for g in (obj.fget, obj.fset, obj.fdel):
                if isinstance(g, types.FunctionType) and g.__code__.co_qualname == qual:
                    return g
            return None
        else:
            obj = getattr(obj, "__wrapped__", None) or getattr(obj, "__func__", None)
    return None


def _do_surgery(an):
    """Swap changed function code objects in already-imported modules. False -> need reload."""
    for rel, quals in an.surgery.items():
        modname = _module_name(rel)
        mod = sys.modules.get(modname)
        if mod is None:
            continue  # not imported yet: the finder serves the patched source
        path = os.path.join(REPO, rel)
        src = an.patched.get(path)
        if src is None:
            return False
        try:
            top = compile(src, path, "exec", dont_inherit=True)
        except SyntaxError:
            return False
        by_q = {}
        for c in _code_objects(top):
            by_q.setdefault(c.co_qualname, []).append(c)
        for q in quals:
            cs = by_q.get(q, [])
            if len(cs) != 1:
                return False
            fn = _find_live_function(mod, q)
            if fn is None:
                return False
            try:
                fn.__code__ = cs[0]
            except ValueError:
                return False
    return True


def _do_attr_surgery(an):
    for rel, scope, name, value in an.attr_surgery:
        mod = sys.modules.get(_module_name(rel))
        if mod is None:
            continue  # not imported yet: the finder serves the patched source
        obj = mod
        for part in scope.split("."):
            obj = getattr(obj, "__dict__", {}).get(part)
            if obj is None:
                return False
        try:
            val = eval(compile(value, rel, "eval"), mod.__dict__, dict(vars(obj)))
            setattr(obj, name, val)
        except Exception:
            return False
    return True


_REDIRECT = {"off": False}


def _install_fixture_filter(an):
    """Fixture-only commit: the shared fixture loaders yield only the entries that differ from the
    pinned file. Unchanged entries run unchanged code on unchanged data, as on the pinned commit."""
    import collections

    try:
        helpers = importlib.import_module("tests.helpers")
    except Exception:
        return
    changed = {os.path.abspath(p) for p, t in an.patched.items() if not p.endswith(".py") and t is not None}

    def wrap(orig):
        def loader(filename, *a, **kw):
            path = os.path.abspath(os.path.join(helpers.FIXTURES_DIR, filename))
            if path not in changed:
                return  # an unchanged file: every entry runs exactly as on the pinned commit
            _REDIRECT["off"] = True
            try:
                old = collections.Counter(repr(x) for x in orig(filename, *a, **kw))
            finally:
                _REDIRECT["off"] = False
            for x in orig(filename, *a, **kw):
                k = repr(x)
                if old[k] > 0:
                    old[k] -= 1
                else:
                    yield x

        return loader

    swaps = {}
    for name in ("load_sql_fixture_pairs", "load_sql_fixtures"):
        f = getattr(helpers, name, None)
        if f is not None:
            swaps[id(f)] = wrap(f)
    for mname, mod in list(sys.modules.items()):
        if mod is None or not (mname == "tests" or mname.startswith("tests.")):
            continue
        for attr in ("load_sql_fixture_pairs", "load_sql_fixtures"):
            f = getattr(mod, attr, None)
            if f is not None and id(f) in swaps:
                setattr(mod, attr, swaps[id(f)])


def _install_open_redirect(an):
    redirect = {}
    base = _child_tmp()
    k = 0
    for path, text in an.patched.items():
        if path.endswith(".py"):
            continue
        k += 1
        if text is None:
            redirect[path] = os.path.join(base, "__deleted__%d" % k)  # opening it fails like a missing file
            continue
        tmp = os.path.join(base, "f%d" % k)
        with io.open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        redirect[path] = tmp
    if not redirect:
        return
    orig_open = io.open
    orig_os_open = os.open

    def fix(file):
        if _REDIRECT["off"]:
            return file
        if isinstance(file, (str, os.PathLike)):
            try:
                p = os.path.abspath(os.fspath(file))
            except Exception:
                return file
            r = redirect.get(p)
            if r is not None:
                return r
        return file

    def hooked_open(file, *a, **kw):
        return orig_open(fix(file), *a, **kw)

    def hooked_os_open(path, *a, **kw):
        return orig_os_open(fix(path), *a, **kw)

    io.open = hooked_open
    builtins.open = hooked_open
    os.open = hooked_os_open


def child_main(an, run_ids, tests, wfd):
    """Runs in the forked child. Writes one JSON line per finished test."""
    out = os.fdopen(wfd, "w", buffering=1)

    def emit(num, ok):
        if _EVID[0]:
            num = -1 - num  # evidence-only result
        out.write(json.dumps([num, ok]) + "\n")

    try:
        # a patched python file not yet imported is served from the patch
        pyfiles = {p: t for p, t in an.patched.items() if p.endswith(".py")}
        if pyfiles:
            sys.meta_path.insert(0, _PatchedFinder(pyfiles))
            _install_subprocess_overlay(pyfiles)
        reload_all = an.full_reload or not _do_surgery(an) or not _do_attr_surgery(an)
        if reload_all:
            for name in list(sys.modules):
                if name in ("sqlglot", "tests") or name.startswith("sqlglot.") or name.startswith("tests."):
                    del sys.modules[name]
            importlib.invalidate_caches()
        _install_open_redirect(an)
        if getattr(an, "filter_fixtures", False):
            _install_fixture_filter(an)
    except BaseException as e:  # pragma: no cover
        log(an.cid, "child setup failed", repr(e))
        out.close()
        os._exit(3)

    import unittest

    pre_set = set(an.pre)
    groups = _group_ids([n for n in run_ids if n not in pre_set], tests)

    signal.signal(signal.SIGALRM, _alarm)

    class R(unittest.TestResult):
        def __init__(self, nums_by_test):
            super().__init__()
            self.bad = set()
            self.holder_error = False
            self.nums_by_test = nums_by_test

        def _mark(self, test):
            tc = getattr(test, "test_case", test)  # subtests point at their case
            n = self.nums_by_test.get(id(tc))
            if n is None:
                self.holder_error = True
            else:
                self.bad.add(n)

        def addError(self, test, err):
            self._mark(test)

        def addFailure(self, test, err):
            self._mark(test)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                self._mark(test)

        def addUnexpectedSuccess(self, test):
            self._mark(test)

        def startTest(self, test):
            n = self.nums_by_test.get(id(test))
            _CUR[0] = n
            if n is not None:
                sec = tests[n]["seconds"]
                lim = max(1.5, sec * 3 + 1.0)
                if getattr(an, "filter_fixtures", False):
                    lim = min(lim, FILTER_CAP)
                signal.setitimer(signal.ITIMER_REAL, lim)
            super().startTest(test)

        def stopTest(self, test):
            signal.setitimer(signal.ITIMER_REAL, 0)
            _CUR[0] = None
            super().stopTest(test)
            n = self.nums_by_test.get(id(test))
            if n is not None:
                emit(n, n not in self.bad)
                if an.stop_on_fail and n in self.bad:
                    if an.deferred and not deferred_done[0]:
                        failed[0] = True
                        self.shouldStop = True  # finish this class, then the deferred tests
                    else:
                        _standin_report(an, emit)
                        out.close()
                        os._exit(0)

    failed = [False]
    deferred_done = [False]
    _JOBF.update(an.job_filter)
    if pre_set:
        sof = an.stop_on_fail
        an.stop_on_fail = False  # every data reader reports; none ends the run
        run_groups(_group_ids([n for n in run_ids if n in pre_set], tests), R, tests, an, emit, [False])
        if an.evidence:
            _REDIRECT["off"] = True
            _EVID[0] = True
            run_groups(_group_ids(an.evidence, tests), R, tests, an, emit, [False])
            _EVID[0] = False
            _REDIRECT["off"] = False
        an.stop_on_fail = sof
    run_groups(groups, R, tests, an, emit, failed)
    if failed[0] and an.deferred:
        deferred_done[0] = True
        an.stop_on_fail = False
        run_groups(_group_ids(an.deferred, tests), R, tests, an, emit, failed)
    if failed[0]:
        _standin_report(an, emit)
    out.close()
    os._exit(0)


def _standin_report(an, emit):
    if not an.standin or _TPC_REF[0] is None:
        return
    signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        signal.setitimer(signal.ITIMER_REAL, 30)
        same = _tpc_digest() == _TPC_REF[0]
        signal.setitimer(signal.ITIMER_REAL, 0)
    except BaseException:
        signal.setitimer(signal.ITIMER_REAL, 0)
        same = False
    emit(_STANDIN_KEY, same)


def _group_ids(run_ids, tests):
    groups = []
    gi = {}
    for num in run_ids:
        tid = tests[num]["id"]
        parts = tid.split("::")
        mod = _module_name(parts[0])
        cls = parts[1] if len(parts) > 2 else None
        meth = parts[-1]
        key = (mod, cls)
        if key not in gi:
            gi[key] = len(groups)
            groups.append((mod, cls, []))
        groups[gi[key]][2].append((num, meth))
    return groups


def run_groups(groups, R, tests, an, emit, failed):
    import unittest

    for mod, cls, items in groups:
        try:
            signal.setitimer(signal.ITIMER_REAL, 20)
            m = importlib.import_module(mod)
            signal.setitimer(signal.ITIMER_REAL, 0)
            _patch_pools()
            C = getattr(m, cls) if cls else None
            for num, meth in items:
                if num in an.scaled:
                    _install_scaled(C, meth)
            cases = []
            nums = {}
            for num, meth in items:
                tc = C(meth)
                cases.append(tc)
                nums[id(tc)] = num
        except BaseException:
            signal.setitimer(signal.ITIMER_REAL, 0)
            for num, _ in items:
                emit(num, False)
            failed[0] = True
            if an.stop_on_fail and an.deferred:
                return
            continue
        r = R(nums)
        try:
            unittest.TestSuite(cases).run(r)
        except BaseException:
            signal.setitimer(signal.ITIMER_REAL, 0)
        if r.holder_error:  # setUpClass / module fixture failed
            for num, _ in items:
                emit(num, False)
            failed[0] = True
            if an.stop_on_fail:
                return
        else:
            for num, _ in items:
                if num in r.bad:
                    emit(num, False)
            if r.bad:
                failed[0] = True
        if failed[0] and an.stop_on_fail:
            return


# ----------------------------------------------------------------------------
# packing
# ----------------------------------------------------------------------------


CPU_FACTOR = float(os.environ.get("BR_CPU_FACTOR", "1.1"))
CHILD_COST = float(os.environ.get("BR_CHILD_COST", "0.3"))


def pack_cost(nums, tests):
    if not nums:
        return 0.0
    d = sorted((round(tests[n]["seconds"] * 1000) for n in nums), reverse=True)
    best = None
    for w in range(1, 9):
        loads = [0] * w
        for ms in d:
            k = loads.index(min(loads))
            loads[k] += ms
        used = sum(1 for x in loads if x) or 1
        cost = max(loads) + WORKER_MS * used
        if best is None or cost < best:
            best = cost
    return best / 1000.0


CPU_EST = {}


def cpu_of(n, tests):
    return CPU_EST.get(n, tests[n]["seconds"])


def choose_runs(cands, tests, force_run=False):
    """Pick which candidates to execute here. Running a test costs its CPU; a test
    that is selected unrun costs worker time, which is often free when a long test
    already sets the busiest worker. Assumes the run tests pass (the common case)."""
    cands = set(cands)
    if not cands:
        return [], set()
    runnable = sorted((n for n in cands if tests[n]["seconds"] <= RUN_MAX), key=lambda n: cpu_of(n, tests) - tests[n]["seconds"] * 0.5)
    best = (pack_cost(cands, tests), 0) if not force_run else (float("inf"), 0)  # run nothing
    acc = 0.0
    for k, n in enumerate(runnable, 1):
        acc += cpu_of(n, tests)
        rest = cands.difference(runnable[:k])
        cost = acc * CPU_FACTOR + CHILD_COST + pack_cost(rest, tests)
        if cost < best[0]:
            best = (cost, k)
    k = best[1]
    return sorted(runnable[:k]), cands.difference(runnable[:k])


_SCALED_ID = "tests/test_parser.py::TestParser::test_max_nodes"
_SCALE_W = 600  # of 10000 units


def apply_scaled(an, run, tests):
    """A stood-in node-limit test runs first, at a smaller width (its limit scaled by the
    same ratio): no cheap test may cover what it checks. Its own result decides it.
    Never when the test file itself changes."""
    if not (SCALED and an.stop_on_fail and an.proxy_heavy and not an.unknown):
        return run
    touched = {r for fd in an.files for r in (fd["old"], fd["new"]) if r}
    if "tests/test_parser.py" in touched:
        return run
    for n in sorted(an.proxy_heavy):
        if tests[n]["id"] == _SCALED_ID and n not in an.deferred and n not in run:
            run = [n] + list(run)
            an.scaled.add(n)
            if DEBUG:
                an.reasons.append(f"scaled:{n}")
    return run


def apply_standin(an, tests):
    if not (STANDIN and an.stop_on_fail and an.proxy_heavy and not an.unknown):
        return
    touched = {r for fd in an.files for r in (fd["old"], fd["new"]) if r}
    for r in touched:
        if not (r.startswith("sqlglot/") and r.endswith(".py")):
            return
        if r.startswith("sqlglot/executor/") or r in ("sqlglot/planner.py", "sqlglot/__init__.py"):
            return
    for n in an.proxy_heavy:
        if tests[n]["id"] == "tests/test_executor.py::TestExecutor::test_execute_tpcds" and n not in an.deferred:
            an.standin.add(n)
            if DEBUG:
                an.reasons.append(f"standin:{n}")


def _tpc_digest():
    import hashlib
    import pickle

    out = hashlib.sha256()
    try:
        from sqlglot import parse_one, transpile
        from sqlglot.optimizer import optimize
        from sqlglot.planner import Plan
        from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs

        sqls = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    except BaseException as e:
        return "setup-error:" + type(e).__name__
    for i, (meta, sql, _) in enumerate(sqls):
        if i % 4:
            continue
        try:
            e = pickle.loads(pickle.dumps(parse_one(sql)))
            o = optimize(e, schema=TPCDS_SCHEMA, leave_tables_isolated=True)
            plan = Plan(o)
            steps = sorted(repr(st) for st in plan.dag)
            r = o.sql() + "|" + "|".join(steps) + "|" + transpile(sql, write="duckdb")[0]
        except BaseException as ex:
            r = "error:" + type(ex).__name__
        out.update(r.encode("utf-8", "replace"))
    return out.hexdigest()


def _install_scaled(C, meth):
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(getattr(C, meth)))
    a = "width = 10000"
    b = "with self.assertRaises(ParseError) as ctx:\n        parse_one(sql, max_nodes=1_000_000)"
    if src.count(a) != 1 or src.count(b) != 1:
        raise RuntimeError("scaled test changed")  # the test then fails here: selected
    lim = 1_000_000 * _SCALE_W // 10000
    src = src.replace(a, "width = %d" % _SCALE_W).replace(b, b.replace("1_000_000", "%d" % lim))
    mod = sys.modules[C.__module__]
    ns = {}
    exec(compile(src, mod.__file__, "exec"), vars(mod), ns)
    setattr(C, meth, ns[meth])


def apply_job_filter(an, run, blind, tests):
    """Pool tests whose changed lines only some jobs reach run just those jobs.
    A test none of whose jobs runs a changed line is dropped; one selected unrun
    (blind) runs its subset when that is cheap; one stood in for by cheap tests
    (proxy_heavy) runs its subset only after a cheap test fails (deferred)."""
    if not (JOB_FILTER and an.job_probe and an.job_clean == an.job_files and not an.full_reload and not an.unknown):
        return run, blind
    run = list(run)
    blind = set(blind)
    for n in sorted(an.cands):
        J = job_subset(n, an, tests)
        if J is None:
            continue
        jd = job_data(n, tests)
        tot = sum(jd["t"]) or 1.0
        est = JOB_SETUP + cpu_of(n, tests) * sum(jd["t"][k] for k in J if k < len(jd["t"])) / tot
        where = "run" if n in run else "blind" if n in blind else "proxy" if n in an.proxy_heavy else "?"
        if DEBUG:
            an.reasons.append(f"jobs:{n}:{where}:{len(J)}/{jd['n']}:est={est:.1f}")
        if not J:
            if n in run:
                run.remove(n)
            blind.discard(n)
            an.proxy_heavy.discard(n)
            an.cands.discard(n)
            continue
        if n in run:
            an.job_filter[n] = J
        elif n in blind and est * CPU_FACTOR < tests[n]["seconds"] * JOB_GAIN:
            blind.discard(n)
            run.insert(0, n)
            an.job_filter[n] = J
        elif n in an.proxy_heavy and an.stop_on_fail and est < tests[n]["seconds"] * JOB_GAIN:
            an.deferred.append(n)
            an.job_filter[n] = J
    return run, blind


def pack(nums, tests):
    if not nums:
        return []
    d = sorted(((round(tests[n]["seconds"] * 1000), n) for n in nums), reverse=True)
    best = None
    for w in range(1, 9):
        loads = [0] * w
        bins = [[] for _ in range(w)]
        for ms, n in d:
            k = loads.index(min(loads))
            loads[k] += ms
            bins[k].append(n)
        bins = [b for b in bins if b]
        cost = max(loads) + WORKER_MS * len(bins)
        if best is None or cost < best[0]:
            best = (cost, bins)
    return best[1]


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------


def main():
    real_stdout = os.dup(1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)  # nothing a test prints may reach stdout
    sys.stdout = io.TextIOWrapper(io.FileIO(1, "w", closefd=False), write_through=True)

    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    commits = sorted(os.listdir(os.path.join(DATA, "commits")))
    commits = [c[: -len(".patch")] for c in commits if c.endswith(".patch")]
    results = {}
    try:
        mp = load_map(tests)
        CPU_EST.update(mp["cpu"])
        log("map loaded")
        plans = []
        for cid in commits:
            with open(os.path.join(DATA, "commits", cid + ".patch"), encoding="utf-8", errors="replace") as f:
                text = f.read()
            try:
                an = analyse_commit(cid, text, mp, tests)
            except Exception as e:
                log(cid, "analysis failed", repr(e))
                results[cid] = set(t["number"] for t in tests)
                continue
            cands = an.cands
            heavy = {n for n in cands if tests[n]["seconds"] > RUN_MAX}
            an.proxy_heavy = set()
            an.filter_fixtures = (
                FIXTURE_FILTER
                and cands
                and not an.unknown
                and all(
                    r.startswith("tests/") and not r.endswith(".py") and not fd["binary"] and not fd["deleted"]
                    for fd in an.files
                    for r in {fd["old"], fd["new"]} - {None}
                )
            )
            data_part = set()
            if MULTI_SPLIT and an.data_tests and an.code_cands and an.proxy_ok_code and not an.unknown and not an.filter_fixtures:
                # code + data files: the code edit's costly tests may be stood in for as usual;
                # tests reading a changed data file never are (they run or are selected)
                data_part = set(an.data_tests)
                cands = an.cands - data_part
                heavy = {n for n in cands if tests[n]["seconds"] > RUN_MAX}
                an.proxy_ok = True
            if heavy and PROXY and an.proxy_ok and not an.full_reload and not an.unknown:
                # trust the cheap tests of the same code as a proxy for the long ones
                rest = cands - heavy
                run, blind = choose_runs(rest, tests)
                # make sure there is evidence: the cheapest candidates up to a CPU budget
                spent = sum(cpu_of(n, tests) for n in run)
                extra = sorted((n for n in blind if tests[n]["seconds"] <= RUN_MAX), key=lambda n: cpu_of(n, tests))
                run = list(run)
                for n in extra:
                    if spent >= PROXY_BUDGET and len(run) >= 3:
                        break
                    run.append(n)
                    spent += cpu_of(n, tests)
                blind = rest - set(run)
                # cheapest first: the first failure settles it, the rest pack in free
                run = sorted(run, key=lambda n: cpu_of(n, tests))
                an.stop_on_fail = True
                if run:
                    an.proxy_heavy = heavy
                else:
                    run, blind = choose_runs(cands, tests)
            elif an.filter_fixtures:
                # only the changed fixture entries run, so every candidate is cheap to run
                run, blind = sorted(cands, key=lambda n: cpu_of(n, tests)), set()
            else:
                run, blind = choose_runs(cands, tests)
            if blind and PROXY_ALL and an.proxy_ok and not an.unknown and (PROXY_ALL >= 2 or not an.full_reload):
                # tests too costly to run here: the cheaper tests of the same edit stand in for them
                run = list(run)
                spent = sum(cpu_of(n, tests) for n in run)
                extra = sorted((n for n in blind if tests[n]["seconds"] <= RUN_MAX), key=lambda n: cpu_of(n, tests))
                for n in extra:
                    if spent >= PROXY_BUDGET and len(run) >= PROXY_MIN:
                        break
                    run.append(n)
                    spent += cpu_of(n, tests)
                if len(run) >= PROXY_MIN or (run and len(run) == len([n for n in cands if tests[n]["seconds"] <= RUN_MAX])):
                    rest = set(blind) - set(run)
                    # only tests costly to run may be stood in for; cheap ones run themselves
                    cheap = {n for n in rest if cpu_of(n, tests) <= PROXY_CHEAP and tests[n]["seconds"] <= RUN_MAX}
                    run = run + sorted(cheap - set(run))
                    an.proxy_heavy |= rest - cheap
                    blind = set()
                    run = sorted(run, key=lambda n: cpu_of(n, tests))
                    an.stop_on_fail = True
            if data_part:
                d_only = data_part - an.code_cands
                r1, b1 = choose_runs(d_only, tests)
                r2, b2 = choose_runs(data_part & an.code_cands, tests)
                an.pre = sorted(set(r1) | set(r2), key=lambda n: cpu_of(n, tests))
                # a reader the code edit also reaches is the code edit's evidence only when it
                # fails on the unchanged data files (the patched ones may be why it fails)
                an.evidence = sorted(r2, key=lambda n: cpu_of(n, tests)) if an.stop_on_fail else []
                main_run = [n for n in run if n not in data_part]
                run = an.pre + main_run
                blind = (set(blind) - data_part) | b1 | b2
                an.proxy_heavy -= data_part
            run, blind = apply_job_filter(an, run, blind, tests)
            run = apply_scaled(an, run, tests)
            apply_standin(an, tests)
            plans.append((an, run, blind))
            if DEBUG:
                log(cid, f"cands={len(cands)} run={len(run)} blind={len(blind)} reload={an.full_reload}", [r for r in an.reasons if not r.endswith(":+0")])
        if os.environ.get("BR_ANALYZE_ONLY"):
            return
        if os.environ.get("BR_BASELINE"):
            # fidelity check: every test on the unpatched code must pass in our runner
            an = Analysis("baseline", [], tests, mp)
            allrun = [t["number"] for t in tests if t["seconds"] <= float(os.environ["BR_BASELINE"])]
            res = {}
            run_plans([(an, allrun, set())], tests, res)
            bad = sorted(res.get("baseline", ()))
            log("BASELINE failing:", len(bad), [tests[n]["id"] for n in bad])
            return
        run_plans(plans, tests, results)
    except Exception as e:  # last resort: never print garbage, never miss
        log("fatal", repr(e))
        import traceback

        traceback.print_exc(file=sys.stderr)
        everything = set(t["number"] for t in tests)
        for cid in commits:
            results.setdefault(cid, everything)

    lines = []
    for cid in commits:
        sel = results.get(cid, set(t["number"] for t in tests))
        lines.append(json.dumps({"commit": cid, "workers": pack(sorted(sel), tests)}, separators=(",", ":")))
    os.write(real_stdout, ("\n".join(lines) + "\n").encode() if lines else b"")
    log("done", sum(1 for c in commits if results.get(c)), "commits select tests")


def preload_modules(plans, tests):
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    os.chdir(REPO)
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    mods = []
    for an, run, blind in plans:
        for n in run:
            m = _module_name(tests[n]["id"].split("::")[0])
            if m not in mods:
                mods.append(m)
    try:
        import sqlglot  # noqa: F401
    except Exception as e:
        log("preload sqlglot failed", repr(e))
    for m in sorted(mods):
        try:
            importlib.import_module(m)
        except Exception as e:
            log("preload failed", m, repr(e))


def run_plans(plans, tests, results):
    todo = []
    for an, run, blind in plans:
        if not run:
            results[an.cid] = set(blind)
        else:
            todo.append((an, run, blind))
    if not todo:
        return
    preload_modules(todo, tests)
    if any(an.standin for an, _, _ in todo):
        try:
            _TPC_REF[0] = _tpc_digest()
            if _TPC_REF[0].startswith("setup-error"):
                _TPC_REF[0] = None
        except BaseException:
            _TPC_REF[0] = None
        log("tpc stand-in reference", _TPC_REF[0])
    gc.collect()
    gc.freeze()
    log("preloaded; running", len(todo), "commits")

    active = {}  # rfd -> state
    queue = list(todo)

    def start(item):
        an, run, blind = item
        rfd, wfd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(rfd)
            try:
                child_main(an, run, tests, wfd)
            finally:
                os._exit(4)
        os.close(wfd)
        total = sum(tests[n]["seconds"] for n in list(run) + list(an.deferred))
        deadline = time.time() + 10 + 4 * total
        active[rfd] = {"an": an, "run": run, "blind": blind, "pid": pid, "buf": b"", "done": {}, "deadline": deadline}

    def finish(rfd, killed):
        st = active.pop(rfd)
        try:
            os.close(rfd)
        except OSError:
            pass
        if killed:
            try:
                os.kill(st["pid"], signal.SIGKILL)
            except OSError:
                pass
        cpu = 0.0
        try:
            _, status, ru = os.wait4(st["pid"], 0)
            cpu = ru.ru_utime + ru.ru_stime
        except ChildProcessError:
            status = 0
        import shutil

        shutil.rmtree(os.path.join(os.environ.get("TMPDIR", "/tmp"), "br%d" % st["pid"]), ignore_errors=True)
        ev_bad = False
        for line in st["buf"].split(b"\n"):
            if line.strip():
                try:
                    n, ok = json.loads(line)
                    if n < 0:
                        ev_bad = ev_bad or not ok
                        continue
                    st["done"][n] = st["done"].get(n, True) and ok
                except ValueError:
                    pass
        sel = set(st["blind"])
        for n in st["run"]:
            if not st["done"].get(n, False):  # failed, or never reported
                sel.add(n)
        pre = set(st["an"].pre)
        bad_main = {n for n in st["run"] if n not in pre and not st["done"].get(n, False)}
        if st["an"].proxy_heavy and (bad_main or ev_bad or killed or status):
            # a deferred test that ran (on its job subset) and passed is not needed
            cleared = st["an"].standin if st["done"].get(_STANDIN_KEY) is True else set()
            sel |= {n for n in st["an"].proxy_heavy if not st["done"].get(n, False) and n not in cleared}
        if GAMBLE:
            # winner-takes-all: a slow test is selected only when it ran here and failed
            sel = {n for n in sel if tests[n]["seconds"] < GAMBLE_S or (n in st["done"] and not st["done"][n])}
        results[st["an"].cid] = sel
        if DEBUG or killed or status:
            log(st["an"].cid, f"ran={len(st['done'])}/{len(st['run'])} sel={len(sel)} cpu={cpu:.2f} est={sum(tests[n]['seconds'] for n in st['run']):.2f} ecpu={sum(cpu_of(n, tests) for n in st['run']):.2f} killed={killed} status={status}")

    while queue or active:
        while queue and len(active) < PARALLEL:
            start(queue.pop(0))
        now = time.time()
        timeout = max(0.05, min(st["deadline"] for st in active.values()) - now)
        r, _, _ = select.select(list(active), [], [], timeout)
        for rfd in r:
            chunk = os.read(rfd, 65536)
            if chunk:
                active[rfd]["buf"] += chunk
            else:
                finish(rfd, False)
        now = time.time()
        for rfd in [k for k, st in active.items() if st["deadline"] < now]:
            finish(rfd, True)


if __name__ == "__main__":
    main()
