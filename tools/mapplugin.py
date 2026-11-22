"""pytest plugin that records, per test, which sqlglot/tests code objects run and
which files are opened. Used once, offline, to build the coverage map from the
pinned sqlglot commit (allowed by the rules: "maps built from the pinned commit").

Run from the sqlglot checkout:
  MAPOUT=/path/out PYTHONPATH=.:/path/to/tools python -m pytest -p mapplugin tests
"""

import builtins
import io
import json
import os
import sys

import pytest

mon = sys.monitoring
TOOL = 4
ROOT = os.path.abspath(".") + os.sep
WATCH = (ROOT + "sqlglot" + os.sep, ROOT + "tests" + os.sep)

_ctx = ["IMPORT"]
_code = {}  # ctx -> set(key)
_opens = {}  # ctx -> set(relpath)
_outcomes = {}


def _key(code):
    f = code.co_filename
    if not os.path.isabs(f):
        f = os.path.abspath(f)
    if not f.startswith(WATCH):
        return None
    return f"{f[len(ROOT):]}::{code.co_qualname}::{code.co_firstlineno}"


def _on_start(code, offset):
    k = _key(code)
    if k is not None:
        s = _code.get(_ctx[0])
        if s is None:
            s = _code[_ctx[0]] = set()
        s.add(k)
    return mon.DISABLE


mon.use_tool_id(TOOL, "blastmap")
mon.register_callback(TOOL, mon.events.PY_START, _on_start)
mon.set_events(TOOL, mon.events.PY_START)

_orig_open = io.open
_orig_os_open = os.open


def _note(file):
    try:
        if isinstance(file, (str, bytes, os.PathLike)):
            p = os.path.abspath(os.fsdecode(file))
            if p.startswith(ROOT):
                _opens.setdefault(_ctx[0], set()).add(p[len(ROOT):])
    except Exception:
        pass


def _hooked_open(file, *a, **k):
    _note(file)
    return _orig_open(file, *a, **k)


def _hooked_os_open(path, *a, **k):
    _note(path)
    return _orig_os_open(path, *a, **k)


io.open = _hooked_open
builtins.open = _hooked_open
os.open = _hooked_os_open


HEAVY = set()
if os.environ.get("HEAVY_IDS"):
    HEAVY = set(json.load(open(os.environ["HEAVY_IDS"])))
_lines = {}  # nodeid -> set("file:line")
_line_target = [None]


def _on_line(code, line):
    tgt = _line_target[0]
    if tgt is not None:
        f = code.co_filename
        if not os.path.isabs(f):
            f = os.path.abspath(f)
        if f.startswith(WATCH):
            tgt.add(f"{f[len(ROOT):]}:{line}")
    return mon.DISABLE


mon.register_callback(TOOL, mon.events.LINE, _on_line)


def _set(ctx):
    _ctx[0] = ctx
    kind, _, nid = ctx.partition("|")
    if nid in HEAVY and kind in ("setup", "call", "teardown"):
        _line_target[0] = _lines.setdefault(nid, set())
        mon.set_events(TOOL, mon.events.PY_START | mon.events.LINE)
    elif _line_target[0] is not None:
        _line_target[0] = None
        mon.set_events(TOOL, mon.events.PY_START)
    mon.restart_events()


@pytest.hookimpl(tryfirst=True)
def pytest_collectstart(collector):
    _set("collect|" + collector.nodeid)


@pytest.hookimpl(tryfirst=True)
def pytest_collectreport(report):
    _set("IMPORT")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    _set("setup|" + item.nodeid)
    yield
    _set("IDLE")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    _set("call|" + item.nodeid)
    yield
    _set("IDLE")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    _set("teardown|" + item.nodeid)
    yield
    _set("IDLE")


class InlineExecutor:
    """Runs pool work in-process so the tracer sees it (tests use spawn pools)."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            import pickle

            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *a, **kw):
        import concurrent.futures as cf

        import pickle

        fut = cf.Future()
        try:
            # spawn workers receive pickled work and return pickled results
            fn, a, kw = pickle.loads(pickle.dumps((fn, a, kw)))
            fut.set_result(pickle.loads(pickle.dumps(fn(*a, **kw))))
        except BaseException as e:
            fut.set_exception(e)
        return fut

    def shutdown(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_spawns = set()


def pytest_collection_finish(session):
    for name, m in list(sys.modules.items()):
        if name.startswith("tests") and hasattr(m, "ProcessPoolExecutor"):
            m.ProcessPoolExecutor = InlineExecutor
    import subprocess

    orig_init = subprocess.Popen.__init__

    def init(self, *a, **kw):
        _spawns.add(_ctx[0].partition("|")[2])
        return orig_init(self, *a, **kw)

    subprocess.Popen.__init__ = init


def pytest_runtest_logreport(report):
    if report.failed:
        _outcomes[report.nodeid] = "failed"
    else:
        _outcomes.setdefault(report.nodeid, "ok")


def pytest_sessionfinish(session):
    out = os.environ.get("MAPOUT")
    if not out:
        return
    os.makedirs(out, exist_ok=True)
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with _orig_open(os.path.join(out, f"map-{wid}.json"), "w") as f:
        json.dump(
            {
                "code": {c: sorted(s) for c, s in _code.items()},
                "opens": {c: sorted(s) for c, s in _opens.items()},
                "outcomes": _outcomes,
                "lines": {n: sorted(s) for n, s in _lines.items()},
                "spawns": sorted(_spawns),
            },
            f,
        )
