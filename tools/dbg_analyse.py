import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import selector as s
D = sys.argv[1]
tests = json.load(open(f"{D}/tests.json"))["tests"]
mp = s.load_map(tests)
for cid in sys.argv[2:]:
    text = open(f"{D}/commits/{cid}.patch").read()
    pf = s.parse_patch(text)[0]
    rel = pf["new"]
    old = open(os.path.join(s.REPO, rel)).read()
    new, ok = s.apply_hunks(old, pf["hunks"])
    r = s.analyse_py(old, new)
    print(cid, ok, r["kind"], r.get("attr_changes"), sorted(r.get("scopes", []))[:5], sorted(r["funcs"])[:5])
    try:
        an = s.analyse_commit(cid, text, mp, tests)
        print("   cands", len(an.cands), an.reasons[:6])
    except Exception as e:
        import traceback; traceback.print_exc()
