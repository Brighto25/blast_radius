"""Cost of selecting every opener of each fixture file (what a fixture edit costs us if unrun)."""
import gzip, json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import selector as s
tests = json.load(open(sys.argv[1]))["tests"]
mp = s.load_map(tests)
rows = []
for rel, ts in mp["opens"].items():
    if not rel.startswith("tests/") or "__pycache__" in rel:
        continue
    heavy = [n for n in ts if tests[n]["seconds"] > s.RUN_MAX]
    rows.append((s.pack_cost(ts, tests), len(ts), rel, [tests[n]["id"].split("::")[-1] for n in heavy]))
rows.sort(reverse=True)
for r in rows[:25]:
    print("%6.2f %4d %s %s" % r)
print("files", len(rows))
