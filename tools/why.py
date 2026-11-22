"""Debug: show the structural classification of a patch. usage: why.py <patch> [...]"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.setdefault("BR_REPO", os.path.expanduser("~/br/sqlglot"))
import selector as S  # noqa: E402

tests = json.load(open(os.path.join(os.path.dirname(__file__), "..", "kit", "sample", "tests.json")))["tests"]
S.MAP_PATH = os.path.join(os.path.dirname(__file__), "..", "map.json.gz")
mp = S.load_map(tests)
for p in sys.argv[1:]:
    for fd in S.parse_patch(open(p).read()):
        rel = fd["new"]
        old = open(os.path.join(S.REPO, rel), encoding="utf-8").read()
        new, ok = S.apply_hunks(old, fd["hunks"])
        if not rel.endswith(".py"):
            continue
        r = S.analyse_py(old, new)
        print(os.path.basename(p), rel, r["kind"], "attr_changes=", r.get("attr_changes"), "scopes=", r.get("scopes"))
        for ch in r.get("attr_changes") or []:
            if ch["kind"] == "scalar":
                rd = mp["names"].get(ch["target"], [])
                print("   readers:", [(u, u in mp["import_units"]) for u in rd][:12])
