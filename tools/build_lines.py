"""Exact line coverage of every test at the pinned commit -> lines.json.gz.

usage: python build_lines.py <lineout_dir> <tests.json> <out.json.gz> [jobout_dir]

Input: lines-*.jsonl from mapplugin.py with ALL_LINES=1 (one record per test phase).
Output: {"ids": [...], "files": {file: "<json>"}} where each file's json is
{"s": [[test idx...], ...], "l": [[line, set index], ...]}: the tests that executed each
line, with identical test sets stored once. A class's setup phase (setUpClass) counts for
every test of that class, like the code map does.

With jobout_dir (tools/run_jobs.sh), pool tests also get per-job data under "jobs":
{test id: "<json>"} with {"n": jobs, "none": [jobs returning None], "t": [secs],
"m": {file: lines every job depends on}, "s": [[job...]], "l": {file: [[line, set]]}}.
"m" holds the class setup, the test's own code outside jobs, and first-time work (lines
the cold run executes in a job but the warm rerun does not: lazy imports, caches).
"""

import gzip
import json
import os
import sys
from collections import defaultdict


def _read(d):
    """(job record, other phases' lines) from one per-job trace directory."""
    job, other = None, set()
    for fn in os.listdir(d):
        if not fn.startswith("lines-"):
            continue
        with open(os.path.join(d, fn)) as fh:
            for row in fh:
                r = json.loads(row)
                if "j" in r:
                    job = r["j"]
                else:
                    other |= {(f, ln) for f, ls in r["l"].items() for ln in ls}
    return job, other


def _pairs(by_file):
    return {(f, ln) for f, ls in by_file.items() for ln in ls}


def build_jobs(jobout, idx):
    out = {}
    cold_root, warm_root = os.path.join(jobout, "cold"), os.path.join(jobout, "warm")
    for m in sorted(os.listdir(cold_root)):
        cold, cold_other = _read(os.path.join(cold_root, m))
        warm, _ = _read(os.path.join(warm_root, m))
        tid = next((t for t in idx if t.endswith("::" + m)), None)
        if cold is None or warm is None or tid is None or cold["n"] != warm["n"]:
            print("skip jobs for", m, file=sys.stderr)
            continue
        n = cold["n"]
        main = cold_other | _pairs(cold["main"]) | _pairs(warm["main"])
        wj = [_pairs(x) for x in warm["jobs"][:n]]
        cj = [_pairs(x) for x in cold["jobs"][:n]]
        for k in range(n):
            main |= cj[k] - wj[k]  # first-time work
        for x in cold["jobs"][n:] + warm["jobs"][n:]:
            main |= _pairs(x)  # after the last submit: the test's own code
        inv = defaultdict(lambda: defaultdict(set))
        for k in range(n):
            for f, ln in wj[k]:
                inv[f][ln].add(k)
        sets, lpairs = {}, {}
        for f, d in inv.items():
            lst = []
            for ln in sorted(d):
                key = tuple(sorted(d[ln]))
                k = sets.get(key)
                if k is None:
                    k = sets[key] = len(sets)
                lst.append([ln, k])
            lpairs[f] = lst
        mf = defaultdict(list)
        for f, ln in main:
            mf[f].append(ln)
        out[tid] = json.dumps(
            {
                "n": n,
                "none": sorted(set(cold["none"]) & set(warm["none"])),
                "t": warm["t"][:n],
                "m": {f: sorted(v) for f, v in mf.items()},
                "s": [list(k) for k in sets],
                "l": lpairs,
            },
            separators=(",", ":"),
        )
        print(f"jobs {m}: n={n} main={len(main)} sets={len(sets)} none={len(cold['none'])}", file=sys.stderr)
    return out


def main():
    lineout, tests_json, out = sys.argv[1:4]
    tests = json.load(open(tests_json))["tests"]
    ids = [t["id"] for t in tests]
    idx = {t: i for i, t in enumerate(ids)}

    def cls_of(nid):
        return nid.rsplit("::", 1)[0]

    own = defaultdict(lambda: defaultdict(set))  # test idx -> file -> lines
    setup = defaultdict(lambda: defaultdict(set))  # class -> file -> lines
    for fn in os.listdir(lineout):
        if not fn.startswith("lines-"):
            continue
        with open(os.path.join(lineout, fn)) as fh:
            for row in fh:
                r = json.loads(row)
                kind, _, nid = r["c"].partition("|")
                i = idx.get(nid)
                if i is None:
                    continue
                dst = setup[cls_of(nid)] if kind == "setup" else own[i]
                for f, ls in r["l"].items():
                    dst[f].update(ls)

    inv = defaultdict(lambda: defaultdict(list))  # file -> line -> [test idx]
    for tid in ids:
        i = idx[tid]
        merged = defaultdict(set)
        for f, ls in own.get(i, {}).items():
            merged[f] |= ls
        for f, ls in setup.get(cls_of(tid), {}).items():
            merged[f] |= ls
        for f, ls in merged.items():
            d = inv[f]
            for ln in ls:
                d[ln].append(i)

    files = {}
    nsets = 0
    for f, d in inv.items():
        sets = {}
        pairs = []
        for ln in sorted(d):
            key = tuple(sorted(d[ln]))
            k = sets.get(key)
            if k is None:
                k = sets[key] = len(sets)
            pairs.append([ln, k])
        nsets += len(sets)
        files[f] = json.dumps({"s": [list(k) for k in sets], "l": pairs}, separators=(",", ":"))
    doc = {"ids": ids, "files": files}
    if len(sys.argv) > 4:
        doc["jobs"] = build_jobs(sys.argv[4], idx)
    with gzip.open(out, "wt", compresslevel=9) as fh:
        json.dump(doc, fh, separators=(",", ":"))
    print(f"files={len(files)} sets={nsets} size={os.path.getsize(out)}", file=sys.stderr)


if __name__ == "__main__":
    main()
