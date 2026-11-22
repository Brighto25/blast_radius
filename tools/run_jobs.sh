#!/bin/bash
# Per-job line traces of the pool tests into ~/br/jobout:
#   cold/<test>: the test alone in a fresh process (what the selector's child sees)
#   warm/<test>: the same test run a second time in one process, caches already built
# Lines the cold run executes but the warm run doesn't (lazy imports, class-level
# caches) are first-time work every job depends on.
set -u
TOOLS="$(cd "$(dirname "$0")" && pwd)"
cd ~/br/sqlglot
rm -rf ~/br/jobout
export PATH=~/br/venv/bin:$PATH
export ALL_LINES=1 SKIP_INTEGRATION=0 PYTHONPATH=.:"$TOOLS"
W=tests/test_zz_warm.py
for m in test_execute_tpch test_execute_tpcds; do
  t=tests/test_executor.py::TestExecutor::$m
  JOB_TESTS=$t MAPOUT=~/br/jobout/cold/$m timeout 900 python -m pytest -q -p mapplugin -p no:cacheprovider "$t" < /dev/null > /dev/null 2>&1
  echo "cold $m rc=$?"
  cat > $W <<PY
from tests.test_executor import TestExecutor as _Base


class TestZZWarm(_Base):
    def test_zz1(self):
        self.$m()

    def test_zz2(self):
        type(self).cache.clear()
        self.$m()
PY
  JOB_TESTS=$W::TestZZWarm::test_zz2 MAPOUT=~/br/jobout/warm/$m timeout 900 python -m pytest -q -p mapplugin -p no:cacheprovider "$W::TestZZWarm::test_zz1" "$W::TestZZWarm::test_zz2" < /dev/null > /dev/null 2>&1
  echo "warm $m rc=$?"
  rm -f $W
done
rm -f $W
ls ~/br/jobout/*/*
