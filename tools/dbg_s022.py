import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import selector as s
D = sys.argv[1]; cid = sys.argv[2]; tname = sys.argv[3]
tests = json.load(open(f"{D}/tests.json"))["tests"]
s.CPU_EST.update({})
mp = s.load_map(tests); s.CPU_EST.update(mp["cpu"])
t = [x for x in tests if x["id"].endswith(tname)][0]
print("test", t["number"], t["seconds"], "cpu_est", s.cpu_of(t["number"], tests))
text = open(f"{D}/commits/{cid}.patch").read()
pf = s.parse_patch(text)[0]; rel = pf["new"]
old = open(os.path.join(s.REPO, rel)).read()
probe = s.probe_lines(old, pf["hunks"])
print("probe", sorted(probe) if probe else probe)
hit = s.line_tests(rel, probe, tests)
print("hit", len(hit), t["number"] in hit)
an = s.analyse_commit(cid, text, mp, tests)
print("cands", len(an.cands), t["number"] in an.cands, an.reasons)
run, blind = s.choose_runs(an.cands, tests)
print("choose_runs run", len(run), "in run", t["number"] in run)
