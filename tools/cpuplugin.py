"""pytest plugin: per-test CPU seconds when pools run in-process (as the selector runs them).

  CPUOUT=dir python -m pytest -p cpuplugin tests
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mapplugin_inline import InlineExecutor  # noqa: E402

_cpu = {}


def pytest_collection_finish(session):
    for name, m in list(sys.modules.items()):
        if name.startswith("tests") and hasattr(m, "ProcessPoolExecutor"):
            m.ProcessPoolExecutor = InlineExecutor


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    t = time.process_time()
    yield
    _cpu[item.nodeid] = _cpu.get(item.nodeid, 0) + time.process_time() - t


def pytest_sessionfinish(session):
    out = os.environ.get("CPUOUT")
    if out:
        os.makedirs(out, exist_ok=True)
        wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
        with open(os.path.join(out, f"cpu-{wid}.json"), "w") as f:
            json.dump(_cpu, f)
