"""Blast Radius selector: choose the tests a patch can break from a coverage map.

The map (maps.json.gz) is built from the pinned sqlglot checkout by build_maps.py:
for every source line, the tests that executed it; for every def/class span, the
tests that executed any line of it; for every data file, the tests that opened it.
"""

import gzip
import io
import json
import os
import re
import sys
import tokenize
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
APP = Path(__file__).resolve().parent
MAP = Path(os.environ.get("BR_MAP", APP / "maps.json.gz"))
SRC = Path(os.environ.get("BR_SRC", APP / "src"))

WORKER_MS = 2000
MAX_WORKERS = 8
ALWAYS_ALL = {"pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini", "conftest.py",
              "tests/conftest.py", "requirements.txt"}
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    """-> [{old, new, hunks: [(old_start, [(tag, line)])]}]"""
    files, cur, hunk = [], None, None
    for raw in text.split("\n"):
        if raw.startswith("diff --git "):
            parts = raw.split(" ")
            a = parts[2][2:] if len(parts) > 2 else ""
            b = parts[3][2:] if len(parts) > 3 else a
            cur = {"old": a, "new": b, "hunks": [], "new_file": False, "deleted": False, "binary": False}
            files.append(cur)
            hunk = None
        elif cur is None:
            continue
        elif hunk is None and raw.startswith("--- "):
            if raw.startswith("--- /dev/null"):
                cur["new_file"] = True
            elif raw.startswith("--- a/"):
                cur["old"] = raw[6:]
        elif hunk is None and raw.startswith("+++ "):
            if raw.startswith("+++ /dev/null"):
                cur["deleted"] = True
            elif raw.startswith("+++ b/"):
                cur["new"] = raw[6:]
        elif raw.startswith("rename from "):
            cur["old"] = raw[12:]
        elif raw.startswith("rename to "):
            cur["new"] = raw[10:]
        elif raw.startswith("new file mode"):
            cur["new_file"] = True
        elif raw.startswith("deleted file mode"):
            cur["deleted"] = True
        elif raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
            cur["binary"] = True
        else:
            m = HUNK.match(raw)
            if m:
                hunk = (int(m.group(1)), [])
                cur["hunks"].append(hunk)
            elif hunk is not None and raw[:1] in (" ", "-", "+"):
                hunk[1].append((raw[0], raw[1:]))
    return files


def changed_lines(f):
    """(removed old line numbers, insertion points as (prev_old, next_old))."""
    removed, inserts = [], []
    for start, lines in f["hunks"]:
        old = start
        pending = False
        for tag, _ in lines:
            if tag == " ":
                pending = False
                old += 1
            elif tag == "-":
                removed.append(old)
                old += 1
                pending = False
            else:
                if not pending:
                    inserts.append((old - 1, old))
                    pending = True
    return removed, inserts


def apply_patch(old_text, f):
    old = old_text.splitlines(keepends=True)
    out, pos = [], 0
    for start, lines in f["hunks"]:
        s = max(start - 1, 0) if lines and any(t != "+" for t, _ in lines) else start
        out.extend(old[pos:s])
        pos = s
        for tag, content in lines:
            if tag == " ":
                out.append(old[pos] if pos < len(old) else content + "\n")
                pos += 1
            elif tag == "-":
                pos += 1
            else:
                out.append(content + "\n")
    out.extend(old[pos:])
    return "".join(out)


def code_tokens(text):
    toks = []
    for t in tokenize.generate_tokens(io.StringIO(text).readline):
        if t.type in (tokenize.COMMENT, tokenize.NL, tokenize.ENDMARKER):
            continue
        toks.append((t.type, t.string))
    return toks


class Map:
    def __init__(self, path):
        with gzip.open(path, "rt") as fh:
            d = json.load(fh)
        self.tests = d["tests"]
        self._sets = d["sets"]
        self._cache = {}
        self.files = d["files"]
        self.data = d["data"]
        self.test_files = d["test_files"]

    def mask(self, i):
        m = self._cache.get(i)
        if m is None:
            m = self._cache[i] = int(self._sets[i], 16)
        return m

    def line_mask(self, rel, line):
        for a, b, s in self.files[rel]["lines"]:
            if a <= line <= b:
                return self.mask(s)
        return 0

    def span_mask(self, rel, line):
        """Tests that ran the innermost def/class around `line`. Outside functions
        (class bodies, module level) also the users of the names that statement defines."""
        e = self.files[rel]
        best = None
        for a, b, kind, s in e["spans"]:
            if a <= line <= b and (best is None or b - a < best[1] - best[0]):
                best = (a, b, kind, s)
        if best is not None and best[2] == "f":
            return self.mask(best[3])
        m = self.mask(best[3]) if best else 0
        st = None
        for a, b, s in e["stmts"]:
            if a <= line <= b and (st is None or b - a < st[1] - st[0]):
                st = (a, b, s)
        if st is not None:
            m |= self.mask(st[2])
        m |= self.mask(e["use"] if best else e["cuse"])
        return m

    def file_mask(self, rel):
        return self.mask(self.files[rel]["all"])

    def all_mask(self):
        return (1 << len(self.tests)) - 1


def py_file_mask(mp, rel, f, source_old):
    """Tests a change to one mapped python file can break."""
    if f["deleted"] or f["new_file"] or f["binary"]:
        return mp.file_mask(rel)
    if source_old is not None:
        try:
            new_text = apply_patch(source_old, f)
            if code_tokens(new_text) == code_tokens(source_old):
                compile(new_text, rel, "exec")
                return 0
            compile(new_text, rel, "exec")
        except Exception:
            return mp.file_mask(rel)  # does not compile / tokenize: every user of the module
    removed, inserts = changed_lines(f)
    m = 0
    for ln in removed:
        lm = mp.line_mask(rel, ln)
        m |= lm if lm else mp.span_mask(rel, ln)
    for prev, nxt in inserts:
        m |= mp.span_mask(rel, max(prev, 1)) | mp.span_mask(rel, max(nxt, 1))
        m |= mp.line_mask(rel, prev) | mp.line_mask(rel, nxt)
    return m


def select(mp, files, extras):
    m = 0
    wide = 0  # files whose full coverage is offered as free filler
    for f in files:
        rel = f["new"] if not f["deleted"] else f["old"]
        old = f["old"]
        if rel in ALWAYS_ALL or old in ALWAYS_ALL:
            return mp.all_mask(), wide
        if rel.endswith(".py") and (old in mp.files or rel in mp.files):
            key = old if old in mp.files else rel
            src = SRC / old
            source_old = src.read_text() if src.exists() else None
            m |= py_file_mask(mp, key, f, source_old)
            wide |= mp.file_mask(key)
            if rel != old and rel in mp.files:
                m |= mp.file_mask(rel)
        elif rel.endswith(".py"):
            # new python file nothing has imported yet
            for name in ("tests/test_dialect_imports.py", "tests/test_dialect_entry_points.py"):
                if name in mp.test_files:
                    m |= mp.mask(mp.test_files[name])
        elif old in mp.data or rel in mp.data:
            key = old if old in mp.data else rel
            m |= mp.mask(mp.data[key])
        elif rel.startswith("sqlglotc/"):
            if "tests/test_sqlglotc.py" in mp.test_files:
                m |= mp.mask(mp.test_files["tests/test_sqlglotc.py"])
    return m, wide


def split(durations, k):
    loads = [0] * k
    bins = [[] for _ in range(k)]
    for n, d in sorted(durations, key=lambda x: -x[1]):
        i = loads.index(min(loads))
        bins[i].append(n)
        loads[i] += d
    return bins, loads


def plan(chosen, filler):
    """chosen/filler: [(number, ms)]. Best worker count, then free filler."""
    if not chosen:
        return []
    best = None
    for k in range(1, min(MAX_WORKERS, len(chosen)) + 1):
        bins, loads = split(chosen, k)
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, bins, loads)
    _, bins, loads = best
    cap = max(loads)
    for n, d in sorted(filler, key=lambda x: x[1]):
        i = loads.index(min(loads))
        if loads[i] + d <= cap:
            bins[i].append(n)
            loads[i] += d
    return bins


def bits(m):
    i = 0
    while m:
        low = m & -m
        yield low.bit_length() - 1
        m ^= low
    return


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    num = {t["id"]: t["number"] for t in tests}
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    mp = Map(MAP)
    number_of = [num.get(t) for t in mp.tests]
    unknown = [t["number"] for t in tests if t["id"] not in set(mp.tests)]
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            files = parse_patch(patch.read_text(errors="replace"))
            m, wide = select(mp, files, None)
        except Exception as e:  # never crash: fall back to everything
            print(f"{patch.stem}: {e!r}", file=sys.stderr)
            m, wide = mp.all_mask(), 0
        chosen = [number_of[i] for i in bits(m) if number_of[i] is not None]
        if m and unknown:
            chosen += unknown
        filler = [number_of[i] for i in bits(wide & ~m) if number_of[i] is not None]
        workers = plan([(n, ms[n]) for n in chosen], [(n, ms[n]) for n in filler])
        print(json.dumps({"commit": patch.stem, "workers": workers}))


if __name__ == "__main__":
    main()
