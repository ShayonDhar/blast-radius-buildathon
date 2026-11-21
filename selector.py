"""Blast Radius selector: choose the tests a patch can break from a coverage map.

The map (maps.json.gz) is built from the pinned sqlglot checkout by build_maps.py:
for every source line, the tests that executed it; for every def/class span, the
tests that executed any line of it; for every data file, the tests that opened it.
"""

import gzip
import json
import os
import sys

DATA = os.environ.get("BR_DATA", "/data")
APP = os.path.dirname(os.path.abspath(__file__))
MAP = os.environ.get("BR_MAP", os.path.join(APP, "maps.json.gz"))
SRC = os.environ.get("BR_SRC", os.path.join(APP, "src"))

WORKER_MS = 2000
MAX_WORKERS = 8
ALWAYS_ALL = {"pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini", "conftest.py",
              "tests/conftest.py", "requirements.txt"}


def parse_hunk_header(raw):
    """'@@ -a,b +c,d @@ ...' -> old start line, or None."""
    if not raw.startswith("@@ -"):
        return None
    head = raw[4:].split(" ", 1)[0]
    try:
        return int(head.split(",")[0])
    except ValueError:
        return None


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
            start = parse_hunk_header(raw)
            if start is not None:
                hunk = (start, [])
                cur["hunks"].append(hunk)
            elif hunk is not None and raw[:1] in (" ", "-", "+"):
                hunk[1].append((raw[0], raw[1:]))
    return files


def changed_lines(f):
    """(removed old line numbers, insertion points as (prev_old, next_old)).
    A replaced line is not an insertion; extra added lines or pure additions are."""
    removed, inserts = [], []
    for start, lines in f["hunks"]:
        old = start
        grp = None  # [first old line, removed count, added count]

        def flush():
            if grp and grp[2] > 0 and (grp[1] == 0 or grp[2] > grp[1]):
                inserts.append((grp[0] - 1, grp[0] + grp[1]))

        for tag, _ in lines:
            if tag == " ":
                flush()
                grp = None
                old += 1
                continue
            if grp is None:
                grp = [old, 0, 0]
            if tag == "-":
                removed.append(old)
                grp[1] += 1
                old += 1
            else:
                grp[2] += 1
        flush()
    return removed, inserts


def insert_texts(f):
    """Added lines of each insertion group, in the same order changed_lines reports them."""
    out = []
    for start, lines in f["hunks"]:
        grp = None
        for tag, content in lines + [(" ", "")]:
            if tag == " ":
                if grp and grp[2] and (grp[1] == 0 or len(grp[2]) > grp[1]):
                    out.append(grp[2])
                grp = None
                continue
            if grp is None:
                grp = [0, 0, []]
            if tag == "-":
                grp[1] += 1
            else:
                grp[2].append(content)
    return out


def apply_patch(old_text, f):
    old = old_text.splitlines(keepends=True)
    out, pos = [], 0
    for start, lines in f["hunks"]:
        s = max(start - 1, 0) if lines and any(t != "+" for t, _ in lines) else start
        out.extend(old[pos:s])
        pos = s
        for tag, content in lines:
            if tag == " " or tag == "-":
                if pos >= len(old) or old[pos].rstrip("\r\n") != content.rstrip("\r"):
                    raise ValueError("patch does not match source")
                if tag == " ":
                    out.append(old[pos])
                pos += 1
            else:
                out.append(content + "\n")
    out.extend(old[pos:])
    return "".join(out)


def code_tokens(text):
    import io
    import tokenize

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
        self._w = d["width"]
        self._cache = {}
        self._raw = d["files"]
        self._files = {}
        self.data = d["data"]
        self.test_files = d["test_files"]

    @property
    def files(self):
        return self

    def __contains__(self, rel):
        return rel in self._raw

    def __getitem__(self, rel):
        e = self._files.get(rel)
        if e is None:
            e = self._files[rel] = json.loads(self._raw[rel])
        return e

    def mask(self, i):
        m = self._cache.get(i)
        if m is None:
            w = self._w
            m = self._cache[i] = int(self._sets[i * w:(i + 1) * w], 16)
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
        cuse = self.mask(e["cuse"])
        # Users of the importing modules, unless that is nearly everything (expressions, helpers).
        m |= cuse if best is None or bin(cuse).count("1") <= 700 else self.mask(e["use"])
        return m

    def in_function(self, rel, line):
        return any(a <= line <= b and k == "f" for a, b, k, _ in self.files[rel]["spans"])

    def stmt_refs(self, rel, line):
        st = None
        for a, b, s in self.files[rel]["stmts"]:
            if a <= line <= b and (st is None or b - a < st[1] - st[0]):
                st = (a, b, s)
        return self.mask(st[2]) if st else 0

    def nonfunc_mask(self, rel, line, source_old=None):
        """Change outside any function body: class attributes / module-level statements."""
        base = rel.rsplit("/", 1)[-1]
        if rel.startswith("tests/") and not base.startswith("test_"):
            return self.all_mask()  # helpers import-time state is shared by every test module
        e = self.files[rel]
        best = None
        for a, b, kind, s in e["spans"]:
            if kind == "c" and a <= line <= b and (best is None or b - a < best[1] - best[0]):
                best = (a, b, s)
        m = self.mask(best[2]) if best else 0
        m |= self.stmt_refs(rel, line)
        cuse = self.mask(e["cuse"])
        m |= cuse if best is None or bin(cuse).count("1") <= 700 else self.mask(e["use"])
        return m

    def file_mask(self, rel):
        return self.mask(self.files[rel]["all"])

    def all_mask(self):
        return (1 << len(self.tests)) - 1


def import_breaking_mask(mp, rel):
    """A module that no longer imports takes down every test importing it."""
    base = rel.rsplit("/", 1)[-1]
    if rel.startswith("sqlglot/") or (rel.startswith("tests/") and not base.startswith("test_")):
        return mp.all_mask()
    sid = mp.test_files.get(rel)
    return mp.mask(sid) if sid is not None else mp.all_mask()


def comment_only(mp, rel, f):
    """True when every changed line is blank or a whole-line comment outside a
    multi-line string and no line continuation can be affected."""
    strs = mp.files[rel]["strs"]
    for start, lines in f["hunks"]:
        old = start
        prev = None
        for tag, content in lines:
            if tag != " ":
                t = content.strip()
                if t and not t.startswith("#"):
                    return False
                if any(a <= old <= b for a, b in strs) or any(a < old - 1 < b for a, b in strs):
                    return False
                if prev is not None and prev.rstrip().endswith("\\"):
                    return False
                if t.startswith("#") and old <= 2:
                    return False  # could be an encoding declaration
            prev = content
            if tag != "+":
                old += 1
    return True


def py_file_mask(mp, rel, f, source_old):
    """Tests a change to one mapped python file can break."""
    if f["deleted"] or f["new_file"] or f["binary"] or rel != f["new"]:
        return import_breaking_mask(mp, rel)
    if f["hunks"] and comment_only(mp, rel, f):
        return 0
    if source_old is not None:
        try:
            new_text = apply_patch(source_old, f)
        except Exception:
            new_text = None
        if new_text is not None:
            try:
                # Parse only, unless a keyword that fails at compile time (not parse time) is touched.
                touched = " ".join(c for _, ls in f["hunks"] for t, c in ls if t != " ")
                full = any(k in touched for k in ("return", "yield", "await", "break", "continue", "nonlocal", "global"))
                compile(new_text, rel, "exec", 0 if full else 0x400)
            except Exception:
                return import_breaking_mask(mp, rel)  # does not even compile
            try:
                head_old = "".join(source_old.splitlines(keepends=True)[:2])
                head_new = "".join(new_text.splitlines(keepends=True)[:2])
                if head_old == head_new or not ("coding:" in head_old or "coding=" in head_old
                                                or "coding:" in head_new or "coding=" in head_new):
                    pass
                else:
                    raise ValueError("encoding declaration changed")
                touches_comment = any("#" in c for _, ls in f["hunks"] for t, c in ls if t != " ")
                if touches_comment and code_tokens(new_text) == code_tokens(source_old):
                    return 0  # comments / blank lines only
            except Exception:
                pass
    removed, inserts = changed_lines(f)
    m = 0
    for ln in removed:
        lm = mp.line_mask(rel, ln)
        if lm and mp.in_function(rel, ln):
            m |= lm
        elif lm:
            m |= lm | mp.stmt_refs(rel, ln)  # class-level line that also runs in tests (lambda entries)
        else:
            m |= mp.span_mask(rel, ln)
    lines_old = source_old.splitlines() if source_old is not None else None
    for (prev, nxt), added in zip(inserts, insert_texts(f)):
        indent = min((len(t) - len(t.lstrip()) for t in added if t.strip()), default=0)
        inner = None
        for a, b, kind, _ in mp.files[rel]["spans"]:
            if kind == "f" and a <= max(prev, 1) and nxt <= b and (inner is None or b - a < inner[1] - inner[0]):
                inner = (a, b)
        body = False
        if inner is not None and lines_old is not None and inner[0] - 1 < len(lines_old):
            hdr = lines_old[inner[0] - 1]
            # first line after decorators that starts with def/async def
            k = inner[0] - 1
            while k < len(lines_old) and not lines_old[k].lstrip().startswith(("def ", "async def ")):
                k += 1
            hdr = lines_old[min(k, len(lines_old) - 1)]
            body = indent > len(hdr) - len(hdr.lstrip())
        elif inner is not None:
            body = True
        if body:
            m |= mp.span_mask(rel, max(prev, 1)) | mp.span_mask(rel, max(nxt, 1))
            m |= mp.line_mask(rel, prev) | mp.line_mask(rel, nxt)
        else:
            m |= mp.nonfunc_mask(rel, max(prev, 1)) | mp.nonfunc_mask(rel, max(nxt, 1))
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
            src = os.path.join(SRC, old)
            source_old = None
            if os.path.exists(src):
                with open(src, encoding="utf-8", errors="replace") as fh:
                    source_old = fh.read()
            m |= py_file_mask(mp, key, f, source_old)
            wide |= mp.file_mask(key)
            if rel != old and rel in mp.files:
                m |= mp.file_mask(rel)
        elif rel.startswith("sqlglotc/") or rel.startswith("sqlglot-integration-tests/"):
            for name in ("tests/test_sqlglotc.py", "tests/test_integration_loader.py"):
                if name in mp.test_files:
                    m |= mp.mask(mp.test_files[name])
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
    with open(os.path.join(DATA, "tests.json"), encoding="utf-8") as fh:
        tests = json.load(fh)["tests"]
    num = {t["id"]: t["number"] for t in tests}
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    try:
        mp = Map(MAP)
        number_of = [num.get(t) for t in mp.tests]
        known = set(mp.tests)
    except Exception as e:  # unreadable map: run everything rather than nothing
        print(f"map unreadable: {e!r}", file=sys.stderr)
        mp, number_of, known = None, [t["number"] for t in tests], {t["id"] for t in tests}
        mp = _AllMap(len(tests))
    unknown = [t["number"] for t in tests if t["id"] not in known]
    cdir = os.path.join(DATA, "commits")
    for name in sorted(n for n in os.listdir(cdir) if n.endswith(".patch")):
        stem = name[:-6]
        try:
            with open(os.path.join(cdir, name), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            files = parse_patch(text)
            if text.strip() and not any(f["hunks"] or f["new_file"] or f["deleted"] or f["old"] != f["new"]
                                        or f["binary"] for f in files):
                m, wide = mp.all_mask(), 0  # a patch we could not read any change from
            else:
                m, wide = select(mp, files, None)
        except Exception as e:  # never crash: fall back to everything
            print(f"{stem}: {e!r}", file=sys.stderr)
            m, wide = mp.all_mask(), 0
        chosen = [number_of[i] for i in bits(m) if number_of[i] is not None]
        if m and unknown:
            chosen += unknown
        filler = [number_of[i] for i in bits(wide & ~m) if number_of[i] is not None]
        workers = plan([(n, ms[n]) for n in chosen], [(n, ms[n]) for n in filler])
        print(json.dumps({"commit": stem, "workers": workers}))


class _AllMap:
    """Stand-in used when the map cannot be read: every change selects every test."""

    def __init__(self, n):
        self._n = n

    def all_mask(self):
        return (1 << self._n) - 1

    def __getattr__(self, name):
        raise RuntimeError("no map")


if __name__ == "__main__":
    main()
