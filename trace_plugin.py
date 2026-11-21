"""pytest plugin used at build time: per-test line coverage + data files opened.

Usage: BR_OUT=<prefix> pytest -p trace_plugin <files>
Writes <prefix>.cov (coverage data with contexts) and <prefix>.opens.json.
Contexts: "C|<module nodeid>" while collecting, "<nodeid>|<phase>" while running.
"""

import json
import os
import pkgutil
import sys

import coverage

_OUT = os.environ["BR_OUT"]
_ROOT = os.getcwd()
_cov = None
_ctx = None
_opens = {}
_used = {}  # ctx -> set of source files whose classes were instantiated/looked up


_cls_files = {}


def _note(cls):
    if _ctx is None:
        return
    files = _cls_files.get(cls)
    if files is None:
        files = set()
        for k in getattr(cls, "__mro__", ()):
            m = sys.modules.get(getattr(k, "__module__", ""))
            f = getattr(m, "__file__", None)
            if f and f.startswith(_ROOT):
                files.add(os.path.relpath(f, _ROOT))
        files = _cls_files[cls] = frozenset(files)
    mods = _used.get(_ctx)
    if mods is None:
        mods = _used[_ctx] = set()
    if not files <= mods:
        mods |= files


def _install_usage_hooks():
    import functools

    from sqlglot.dialects import dialect as D
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    for base in ():
        orig = base.__init__

        def make(orig):
            @functools.wraps(orig)
            def init(self, *a, **k):
                _note(type(self))
                return orig(self, *a, **k)

            return init

        base.__init__ = make(orig)
    orig = D.Dialect.get_or_raise.__func__

    def gor(cls, *a, **k):
        r = orig(cls, *a, **k)
        _note(type(r))
        return r

    D.Dialect.get_or_raise = classmethod(gor)


def _preimport():
    import sqlglot

    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name.endswith("__main__"):
            continue
        try:
            __import__(m.name)
        except Exception as e:  # pragma: no cover
            print("preimport failed", m.name, e, file=sys.stderr)


class _Tagged:
    """Wraps a task sent to a worker process so the worker records coverage
    under the context of the test that submitted it."""

    def __init__(self, ctx, fn):
        self.ctx, self.fn = ctx, fn

    def __call__(self, *a, **k):
        global _cov, _ctx
        if _cov is None:
            _preimport()
            _install_usage_hooks()
            _cov = coverage.Coverage(
                data_file=f"{_OUT}.w{os.getpid()}.cov",
                include=[os.path.join(_ROOT, "sqlglot", "*"), os.path.join(_ROOT, "tests", "*")],
                config_file=False,
            )
            _cov.start()
        _switch(self.ctx)
        try:
            return self.fn(*a, **k)
        finally:
            _switch("")
            _cov.save()
            with open(f"{_OUT}.w{os.getpid()}.used.json", "w") as f:
                json.dump({k: sorted(v) for k, v in _used.items()}, f)


def _patch_pools():
    from concurrent.futures import ProcessPoolExecutor

    orig = ProcessPoolExecutor.submit

    def submit(self, fn, /, *a, **k):
        return orig(self, _Tagged(_ctx or "", fn), *a, **k)

    ProcessPoolExecutor.submit = submit


def _audit(event, args):
    if event != "open" or _ctx is None:
        return
    path = args[0]
    if not isinstance(path, str):
        return
    mode = args[1] if len(args) > 1 else None
    if isinstance(mode, str) and any(c in mode for c in "wa+x"):
        return
    ap = os.path.abspath(path)
    if ap.startswith(_ROOT + os.sep + "tests" + os.sep) or "/tests/" in ap:
        _opens.setdefault(_ctx, set()).add(os.path.relpath(ap, _ROOT))


def _switch(ctx):
    global _ctx
    _ctx = ctx
    if _cov is not None:
        _cov.switch_context(ctx)


def pytest_configure(config):
    global _cov
    # Import every sqlglot module first so import-time lines are not charged to
    # whichever test happens to import a dialect lazily.
    _preimport()
    _cov = coverage.Coverage(
        data_file=_OUT + ".cov",
        include=[os.path.join(_ROOT, "sqlglot", "*"), os.path.join(_ROOT, "tests", "*")],
        config_file=False,
        branch=False,
    )
    _install_usage_hooks()
    _patch_pools()
    _cov.start()
    sys.addaudithook(_audit)


def pytest_collectstart(collector):
    _switch("C|" + collector.nodeid)


import pytest  # noqa: E402

pytest_runtest_setup = pytest.hookimpl(wrapper=True)(
    lambda item: (yield from _wrap(item, "setup"))
)
pytest_runtest_call = pytest.hookimpl(wrapper=True)(
    lambda item: (yield from _wrap(item, "call"))
)
pytest_runtest_teardown = pytest.hookimpl(wrapper=True)(
    lambda item, nextitem: (yield from _wrap(item, "teardown"))
)


def _wrap(item, name):
    _switch(item.nodeid + "|" + name)
    try:
        return (yield)
    finally:
        _switch("")


def pytest_sessionfinish(session, exitstatus):
    _cov.stop()
    _cov.save()
    with open(_OUT + ".opens.json", "w") as f:
        json.dump({"opens": {k: sorted(v) for k, v in _opens.items()},
                   "used": {k: sorted(v) for k, v in _used.items()}}, f)
