"""Runtime verifier: run selected sqlglot tests against a patched copy of the source.

The parent process imports the heavy third-party modules once (pytest, duckdb, pandas) and
then forks one child per call. The child sees an overlay directory: a symlink farm of the
pristine tree with the patched files written as real files, so only patched modules compile.
"""

import json
import os
import resource
import shutil
import sys
import time

_seq = 0


class Verifier:
    def __init__(self, src_root, timeout_s=20, tmp="/tmp"):
        self.src = os.path.abspath(src_root)
        self.timeout_s = timeout_s
        self.tmp = tmp
        self.started = False

    # -- zygote -------------------------------------------------------------------------------
    def start(self):
        import pytest  # noqa: F401

        for name in ("pytest_timeout", "duckdb", "pandas", "_pytest.unittest", "_pytest.subtests",
                     "unittest", "concurrent.futures", "multiprocessing", "json", "decimal", "datetime"):
            try:
                __import__(name)
            except Exception:
                pass
        self.started = True

    # -- overlay ------------------------------------------------------------------------------
    def _build_overlay(self, patch_files):
        global _seq
        _seq += 1
        ov = os.path.join(self.tmp, f"ov_{os.getpid()}_{_seq}")
        os.makedirs(ov)
        # directories that must be real (ancestors of a patched file); all others are symlinks
        real_dirs = {""}
        for rel in patch_files:
            parts = rel.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                real_dirs.add("/".join(parts[:i]))

        def mirror(rel_dir):
            src_dir = os.path.join(self.src, rel_dir)
            dst_dir = os.path.join(ov, rel_dir)
            if rel_dir:
                os.makedirs(dst_dir, exist_ok=True)
            for name in os.listdir(src_dir):
                rel = f"{rel_dir}/{name}" if rel_dir else name
                if rel in patch_files:
                    continue
                if rel in real_dirs and os.path.isdir(os.path.join(src_dir, name)):
                    mirror(rel)
                else:
                    os.symlink(os.path.join(src_dir, name), os.path.join(dst_dir, name))

        mirror("")
        for rel, text in patch_files.items():
            if text is None:
                continue
            p = os.path.join(ov, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        return ov

    # -- run ----------------------------------------------------------------------------------
    def run(self, patch_files, test_ids, expected_seconds=None, stop_first=False):
        """-> (failing node ids or file/collector ids, ok, cpu_seconds)."""
        if not self.started:
            self.start()
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        ov = None
        try:
            ov = self._build_overlay(patch_files)
            res_path = ov + ".json"
            wall = 5 + 3 * (expected_seconds if expected_seconds is not None else len(test_ids))
            sys.stdout.flush()
            sys.stderr.flush()
            pid = os.fork()
            if pid == 0:  # child
                code = 1
                try:
                    code = _child(ov, test_ids, res_path, self.timeout_s, stop_first)
                finally:
                    os._exit(code)
            deadline = time.monotonic() + wall
            status = None
            while True:
                done, st = os.waitpid(pid, os.WNOHANG)
                if done:
                    status = st
                    break
                if time.monotonic() > deadline:
                    try:
                        os.killpg(pid, 9)
                    except Exception:
                        pass
                    try:
                        os.kill(pid, 9)
                    except Exception:
                        pass
                    os.waitpid(pid, 0)
                    break
                time.sleep(0.005)
            failing, ok = set(), False
            if status is not None and os.path.exists(res_path):
                with open(res_path) as fh:
                    d = json.load(fh)
                failing = set(d["failed"])
                ok = d["complete"]
        except Exception as e:  # pragma: no cover
            print(f"verifier error: {e!r}", file=sys.stderr)
            failing, ok = set(), False
        finally:
            if ov:
                shutil.rmtree(ov, ignore_errors=True)
                try:
                    os.unlink(ov + ".json")
                except OSError:
                    pass
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
        return failing, ok, cpu

    def close(self):
        pass


class _SyncExecutor:
    """Stand-in for ProcessPoolExecutor inside the verifier child: runs tasks in-process
    (spawning workers would re-import sqlglot per worker and dominate CPU)."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        if initializer is not None:  # a worker would run this once, on unpickled arguments
            import pickle

            initializer(*pickle.loads(pickle.dumps(tuple(initargs))))

    def submit(self, fn, /, *args, **kwargs):
        import pickle
        from concurrent.futures import Future

        fut = Future()
        try:
            # same pickling round trip a worker process would do (serde changes must show up)
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            fut.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except BaseException as e:
            fut.set_exception(e)
        return fut

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return [self.submit(fn, *a).result() for a in zip(*iterables)]

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Recorder:
    def __init__(self):
        self.failed = set()
        self.ran = set()
        self.collect_errors = set()

    def pytest_runtest_logreport(self, report):
        self.ran.add(report.nodeid)
        if report.failed:
            self.failed.add(report.nodeid)

    def pytest_collectreport(self, report):
        if report.failed:
            self.failed.add(report.nodeid)
            self.collect_errors.add(report.nodeid)


def _child(ov, test_ids, res_path, timeout_s, stop_first=False):
    try:
        os.setpgid(0, 0)
    except Exception:
        pass
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    os.chdir(ov)
    sys.path.insert(0, ov)
    # spawned pool workers must not re-import the selector script as __mp_main__
    main = sys.modules.get("__main__")
    if main is not None:
        main.__dict__.pop("__file__", None)
        main.__spec__ = None
    for m in [m for m in sys.modules if m == "sqlglot" or m.startswith(("sqlglot.", "tests"))]:
        del sys.modules[m]
    import concurrent.futures as cf

    cf.ProcessPoolExecutor = _SyncExecutor
    import pytest

    rec = _Recorder()
    args = [*test_ids, "-q", "-p", "no:cacheprovider", "-p", "no:randomly", "-p", "no:xdist",
            "--timeout", str(timeout_s), "-o", "addopts=", "--rootdir", ov, "--assert=plain"]
    if stop_first:
        args.append("-x")
    try:
        rc = pytest.main(args, plugins=[rec])
    except BaseException:
        rc = -1
    complete = rc in (0, 1)  # 0 all passed, 1 some failed; anything else = unreliable
    with open(res_path, "w") as fh:
        json.dump({"failed": sorted(rec.failed), "complete": complete, "rc": int(rc) if isinstance(rc, int) else -1,
                   "ran": len(rec.ran)}, fh)
    return 0
