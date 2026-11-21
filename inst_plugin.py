"""pytest plugin used at build time: which sqlglot expression classes each test instantiates.

Usage: BR_OUT=<prefix> pytest -p inst_plugin <files>
Writes <prefix>.<pid>.inst.json: {context: [class keys]} where a class key is "file|Qualname" for
every sqlglot class in the instantiated type's MRO. Contexts: "C|<collector nodeid>" while collecting,
"<nodeid>|<phase>" while running. Class-level attributes such as arg_types are consumed when the class
is created, so line coverage cannot say which tests depend on them; instantiation can.
"""

import json
import os
import sys

_OUT = os.environ["BR_OUT"]
_ROOT = os.getcwd()
_ctx = None
_inst = {}  # ctx -> set of types
_installed = False


def _install():
    global _installed
    if _installed:
        return
    from sqlglot.expressions import core

    orig = core.Expr.__init__

    def init(self, *a, **k):
        c = _ctx
        if c is not None:
            s = _inst.get(c)
            if s is None:
                s = _inst[c] = set()
            s.add(type(self))
        return orig(self, *a, **k)

    core.Expr.__init__ = init
    _installed = True


_keys = {}


def _class_keys(cls):
    ks = _keys.get(cls)
    if ks is None:
        out = set()
        for k in cls.__mro__:
            f = getattr(sys.modules.get(k.__module__), "__file__", None)
            if f and f.startswith(_ROOT + os.sep):
                out.add(os.path.relpath(f, _ROOT) + "|" + k.__qualname__)
        ks = _keys[cls] = out
    return ks


def _dump(path):
    with open(path, "w") as fh:
        json.dump({c: sorted(set().union(*(_class_keys(t) for t in ts))) for c, ts in _inst.items()}, fh)


class _Tagged:
    """Runs a pool task under the submitting test's context inside the worker process."""

    def __init__(self, ctx, fn):
        self.ctx, self.fn = ctx, fn

    def __call__(self, *a, **k):
        global _ctx
        _install()
        _ctx = self.ctx
        try:
            return self.fn(*a, **k)
        finally:
            _ctx = None
            _dump(f"{_OUT}.w{os.getpid()}.inst.json")


def pytest_configure(config):
    _install()
    from concurrent.futures import ProcessPoolExecutor

    orig = ProcessPoolExecutor.submit

    def submit(self, fn, /, *a, **k):
        return orig(self, _Tagged(_ctx or "", fn), *a, **k)

    ProcessPoolExecutor.submit = submit


def pytest_collectstart(collector):
    global _ctx
    _ctx = "C|" + collector.nodeid


import pytest  # noqa: E402


def _wrap(item, name):
    global _ctx
    _ctx = item.nodeid + "|" + name
    try:
        return (yield)
    finally:
        _ctx = None


pytest_runtest_setup = pytest.hookimpl(wrapper=True)(lambda item: (yield from _wrap(item, "setup")))
pytest_runtest_call = pytest.hookimpl(wrapper=True)(lambda item: (yield from _wrap(item, "call")))
pytest_runtest_teardown = pytest.hookimpl(wrapper=True)(
    lambda item, nextitem: (yield from _wrap(item, "teardown"))
)


def pytest_sessionfinish(session, exitstatus):
    _dump(f"{_OUT}.{os.getpid()}.inst.json")
