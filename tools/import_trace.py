"""Offline: (1) which sqlglot/tests code runs while importing the test modules,
(2) which sqlglot modules each test module pulls in at import time.

usage (from sqlglot checkout): python import_trace.py <tests.json> <out_dir>
"""

import json
import os
import subprocess
import sys

tests_json, out_dir = sys.argv[1], sys.argv[2]
os.makedirs(out_dir, exist_ok=True)
tests = json.load(open(tests_json))["tests"]
mods = sorted({t["file"][:-3].replace("/", ".") for t in tests})

if os.environ.get("ONE_MODULE"):
    m = os.environ["ONE_MODULE"]
    sys.path.insert(0, os.getcwd())
    __import__(m)
    print(json.dumps(sorted(k for k in sys.modules if k == "sqlglot" or k.startswith(("sqlglot.", "tests.")))))
    sys.exit(0)

if os.environ.get("TRACE_ALL"):
    ROOT = os.path.abspath(".") + os.sep
    WATCH = (ROOT + "sqlglot" + os.sep, ROOT + "tests" + os.sep)
    keys = set()
    mon = sys.monitoring

    def cb(code, off):
        f = os.path.abspath(code.co_filename)
        if f.startswith(WATCH):
            keys.add(f"{f[len(ROOT):]}::{code.co_qualname}::{code.co_firstlineno}")
        return mon.DISABLE

    mon.use_tool_id(4, "imp")
    mon.register_callback(4, mon.events.PY_START, cb)
    mon.set_events(4, mon.events.PY_START)
    sys.path.insert(0, os.getcwd())
    import pkgutil

    import sqlglot  # noqa

    for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        try:
            __import__(info.name)
        except Exception as e:
            print("lib import failed", info.name, e, file=sys.stderr)
    json.dump(sorted(keys), open(os.path.join(out_dir, "lib_import_trace.json"), "w"))
    mon.restart_events()

    for m in mods:
        try:
            __import__(m)
        except Exception as e:
            print("import failed", m, e, file=sys.stderr)
    mon.set_events(4, 0)
    json.dump(sorted(keys), open(os.path.join(out_dir, "import_trace.json"), "w"))
    sys.exit(0)

env = dict(os.environ, TRACE_ALL="1", SKIP_INTEGRATION="0")
subprocess.run([sys.executable, __file__, tests_json, out_dir], env=env, check=True)
importers = {}
for m in mods:
    env = dict(os.environ, ONE_MODULE=m, SKIP_INTEGRATION="0")
    r = subprocess.run([sys.executable, __file__, tests_json, out_dir], env=env, capture_output=True, text=True)
    if r.returncode:
        print("fail", m, r.stderr[-300:], file=sys.stderr)
        continue
    importers[m] = json.loads(r.stdout.strip().splitlines()[-1])
json.dump(importers, open(os.path.join(out_dir, "importers.json"), "w"))
print("ok", len(importers))
