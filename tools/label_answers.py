import json, glob, os, sys
S = sys.argv[1]
tests = json.load(open(f"{S}/tests.json"))["tests"]
num = {t["id"]: t["number"] for t in tests}
kinds = json.load(open(f"{S}/kinds.json"))
ans = {}
for cid, k in kinds.items():
    if not os.path.exists(f"{S}/labels/{cid}.done"):
        continue
    failed = set()
    for f in glob.glob(f"{S}/labels/{cid}.*.json"):
        failed |= set(json.load(open(f)))
    nums = set()
    for n in failed:
        if n.startswith("COLLECT::"):
            mod = n[len("COLLECT::"):]
            nums |= {t["number"] for t in tests if t["id"].startswith(mod)}
        elif n in num:
            nums.add(num[n])
    ans[cid] = {"failing": sorted(nums), "kind": k["kind"]}
json.dump({"commits": ans}, open(f"{S}/answers.json", "w"))
print("labeled", len(ans))
