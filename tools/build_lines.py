"""Exact line coverage of every test at the pinned commit -> lines.json.gz.

usage: python build_lines.py <lineout_dir> <tests.json> <out.json.gz>

Input: lines-*.jsonl from mapplugin.py with ALL_LINES=1 (one record per test phase).
Output: {"ids": [...], "files": {file: "<json>"}} where each file's json is
{"s": [[test idx...], ...], "l": [[line, set index], ...]}: the tests that executed each
line, with identical test sets stored once. A class's setup phase (setUpClass) counts for
every test of that class, like the code map does.
"""

import gzip
import json
import os
import sys
from collections import defaultdict


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
    with gzip.open(out, "wt", compresslevel=9) as fh:
        json.dump({"ids": ids, "files": files}, fh, separators=(",", ":"))
    print(f"files={len(files)} sets={nsets} size={os.path.getsize(out)}", file=sys.stderr)


if __name__ == "__main__":
    main()
