"""pytest plugin: record failing test node ids (failures, errors, subtest failures)."""
import json
import os

_failed = set()


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _failed.add("COLLECT::" + report.nodeid)


def pytest_sessionfinish(session):
    out = os.environ.get("LABELOUT")
    if out:
        wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
        with open(f"{out}.{wid}.json", "w") as f:
            json.dump(sorted(_failed), f)
