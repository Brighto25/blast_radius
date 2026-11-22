"""Turn raw per-worker traces from mapplugin.py into map.json.gz.

usage: python build_map.py <mapout_dir> <import_trace.json> <sqlglot_checkout> <tests.json> <out.json.gz>

Everything here is derived from running sqlglot's own tests at the pinned commit
(coverage/import/fixture maps), never from the sample patches or answers.
"""

import gzip
import json
import os
import sys
import types
from collections import defaultdict


def unit_of(key):
    # "sqlglot/x.py::Qual.name::123" -> "sqlglot/x.py::Qual.name"
    f, q, _ = key.rsplit("::", 2)
    return f + "::" + q


def main():
    mapout, import_trace, repo, tests_json, out = sys.argv[1:6]
    tests = json.load(open(tests_json))["tests"]
    ids = [t["id"] for t in tests]
    idx = {t: i for i, t in enumerate(ids)}

    code = defaultdict(set)  # ctx -> keys
    opens = defaultdict(set)
    outcomes = {}
    lines = defaultdict(set)
    spawns = set()
    for fn in os.listdir(mapout):
        d = json.load(open(os.path.join(mapout, fn)))
        spawns.update(n for n in d.get("spawns", ()) if n in idx)
        for n, ls in d.get("lines", {}).items():
            lines[n].update(ls)
        for c, ks in d["code"].items():
            code[c].update(ks)
        for c, ps in d["opens"].items():
            opens[c].update(ps)
        outcomes.update(d["outcomes"])

    failed = [k for k, v in outcomes.items() if v == "failed"]
    print("failed during mapping:", failed, file=sys.stderr)

    # class-level setup (setUpClass) runs inside the first test's setup phase:
    # give every test of that class the union of all setup phases of the class.
    def cls_of(nodeid):
        return nodeid.rsplit("::", 1)[0]

    def mod_of(nodeid):
        return nodeid.split("::", 1)[0]

    setup_by_class_code = defaultdict(set)
    setup_by_class_open = defaultdict(set)
    collect_open_by_mod = defaultdict(set)
    import_keys = set()
    for c, ks in code.items():
        kind, _, nid = c.partition("|")
        if kind == "setup":
            setup_by_class_code[cls_of(nid)].update(ks)
        elif kind in ("IMPORT", "collect"):
            import_keys.update(ks)
    for c, ps in opens.items():
        kind, _, nid = c.partition("|")
        if kind == "setup":
            setup_by_class_open[cls_of(nid)].update(ps)
        elif kind == "collect":
            collect_open_by_mod[mod_of(nid)].update(ps)

    unit_tests = defaultdict(set)
    open_tests = defaultdict(set)
    for tid in ids:
        i = idx[tid]
        ks = set(setup_by_class_code.get(cls_of(tid), ()))
        ps = set(setup_by_class_open.get(cls_of(tid), ())) | collect_open_by_mod.get(mod_of(tid), set())
        for ph in ("setup", "call", "teardown"):
            ks |= code.get(f"{ph}|{tid}", set())
            ps |= opens.get(f"{ph}|{tid}", set())
        for k in ks:
            unit_tests[unit_of(k)].add(i)
        for p in ps:
            open_tests[p].add(i)

    # import-time units: from pytest collection plus a full eager import trace
    imp = {unit_of(k) for k in import_keys}
    if os.path.exists(import_trace):
        imp |= {unit_of(k) for k in json.load(open(import_trace))}

    # name -> units whose code mentions that name (attribute/global/const string)
    names = defaultdict(set)
    walk = list(os.walk(os.path.join(repo, "sqlglot"))) + list(os.walk(os.path.join(repo, "tests")))
    for root, _, files in walk:
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
                u = rel + "::" + co.co_qualname
                for n in co.co_names:
                    names[n].add(u)
                for cst in co.co_consts:
                    if isinstance(cst, types.CodeType):
                        stack.append(cst)
                    elif isinstance(cst, str) and cst.isidentifier() and len(cst) < 60:
                        names[cst].add(u)

    # sqlglot file -> sqlglot files that import it (for dialect subclassing)
    import ast as _ast

    file_importers = defaultdict(set)
    for root, _, files in os.walk(os.path.join(repo, "sqlglot")):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, repo).replace(os.sep, "/")
            try:
                tree = _ast.parse(open(path, encoding="utf-8").read())
            except SyntaxError:
                continue
            for n in _ast.walk(tree):
                if isinstance(n, _ast.ImportFrom) and n.module and n.level == 0:
                    target = n.module.replace(".", "/")
                    for cand in (target + ".py", target + "/__init__.py"):
                        if os.path.exists(os.path.join(repo, cand)) and cand != rel:
                            file_importers[cand].add(rel)
                    for a in n.names:
                        sub = target + "/" + a.name + ".py"
                        if os.path.exists(os.path.join(repo, sub)) and sub != rel:
                            file_importers[sub].add(rel)

    file_tests = defaultdict(set)
    for u, ts in unit_tests.items():
        file_tests[u.split("::", 1)[0]].update(ts)

    # module -> tests whose test module imports it at import time
    module_importers = defaultdict(set)
    imp_path = os.path.join(os.path.dirname(import_trace), "importers.json")
    if os.path.exists(imp_path):
        tests_by_mod = defaultdict(set)
        for t in tests:
            tests_by_mod[t["file"][:-3].replace("/", ".")].add(idx[t["id"]])
        for tm, loaded in json.load(open(imp_path)).items():
            for m in loaded:
                module_importers[m] |= tests_by_mod.get(tm, set())

    # line coverage of slow tests: {idx: {file: [lines]}}, and the units their
    # class-level setup touches (setup lines may be recorded on another test)
    heavy_lines = {}
    heavy_setup_units = {}
    for nid, ls in lines.items():
        if nid not in idx:
            continue
        byf = defaultdict(list)
        for s in ls:
            f, _, ln = s.rpartition(":")
            byf[f].append(int(ln))
        heavy_lines[idx[nid]] = {f: sorted(v) for f, v in byf.items()}
        heavy_setup_units[idx[nid]] = sorted({unit_of(k) for k in setup_by_class_code.get(cls_of(nid), ())})

    # measured in-process CPU per test, scaled to the recorded seconds' machine
    cpu_est = {}
    cpu_dir = os.environ.get("CPUOUT")
    if cpu_dir and os.path.isdir(cpu_dir):
        import statistics

        meas = {}
        for fn in os.listdir(cpu_dir):
            meas.update(json.load(open(os.path.join(cpu_dir, fn))))
        ratios = [meas[t["id"]] / t["seconds"] for t in tests if t["id"] in meas and t["seconds"] > 0.05]
        scale = statistics.median(ratios) if ratios else 1.0
        for t in tests:
            if t["id"] in meas:
                cpu_est[idx[t["id"]]] = round(meas[t["id"]] / scale, 4)
        print("cpu scale", round(scale, 3), file=sys.stderr)

    data = {
        "ids": ids,
        "cpu": cpu_est,
        "spawn_tests": sorted(idx[n] for n in spawns),
        "file_importers": {f: sorted(s) for f, s in file_importers.items()},
        "heavy_lines": heavy_lines,
        "heavy_setup_units": heavy_setup_units,
        "module_importers": {m: sorted(ts) for m, ts in module_importers.items()},
        "units": {u: sorted(ts) for u, ts in unit_tests.items()},
        "files": {f: sorted(ts) for f, ts in file_tests.items()},
        "opens": {p: sorted(ts) for p, ts in open_tests.items()},
        "import_units": sorted(imp),
        "names": {n: sorted(us) for n, us in names.items() if len(us) < 400},
    }
    with gzip.open(out, "wt", compresslevel=9) as f:
        json.dump(data, f, separators=(",", ":"))
    print(
        f"units={len(unit_tests)} files={len(file_tests)} opens={len(open_tests)} import_units={len(imp)} size={os.path.getsize(out)}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
