"""Blast Radius selector: choose the tests a patch can break from a coverage map.

The map (maps.json.gz) is built from the pinned sqlglot checkout by build_maps.py:
for every source line, the tests that executed it; for every def/class span, the
tests that executed any line of it; for every data file, the tests that opened it.
"""

import gzip
import json
import os
import sys
import time

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
        n = len(self._sets) // self._w
        bad = [f for f, i in self.test_files.items() if i >= n]
        if bad:  # ids past the stored sets: rebuild those masks from the test ids
            self._extra = {}
            for f in bad:
                m = 0
                for k, t in enumerate(self.tests):
                    if t.startswith(f + "::"):
                        m |= 1 << k
                self._extra[n + len(self._extra)] = m
                self.test_files[f] = n + len(self._extra) - 1
            self._cache.update(self._extra)
        self.attrs = d.get("attrs", {})
        self.inst = d.get("inst", {})
        self.impcls = set(d.get("impcls", ()))
        self.impattrs = set(d.get("impattrs", ()))

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
        du = self.dialect_users(rel) if NARROW else None
        if du is not None:
            return m | du
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

    def is_dead(self, rel, line):
        """Executable function-body line that no test ever ran."""
        return any(a <= line <= b for a, b in self.files[rel].get("dead", ()))

    def attr_mask(self, rel, line):
        """Tests that read the class attribute(s) assigned on `line` at test time, or None when the
        statement is not a probed class attribute (or an attribute no test ever read)."""
        keys = None
        for a, b, qual, names in self.files[rel].get("cstmts", ()):
            if a <= line <= b:
                keys = [rel + "|" + qual + "|" + n for n in names]
                break
        if not keys:
            return None
        m = 0
        for k in keys:
            sid = self.attrs.get(k)
            if sid is None:
                return None
            m |= self.mask(sid)
        return m

    def in_function(self, rel, line):
        return any(a <= line <= b and k == "f" for a, b, k, _ in self.files[rel]["spans"])

    def stmt_refs(self, rel, line):
        st = None
        for a, b, s in self.files[rel]["stmts"]:
            if a <= line <= b and (st is None or b - a < st[1] - st[0]):
                st = (a, b, s)
        return self.mask(st[2]) if st else 0

    def dialect_users(self, rel):
        """For sqlglot/{dialects,parsers,generators,typing}/NAME.py of a concrete dialect:
        tests that used that dialect (get_or_raise MRO) or ran lines of its files; else None."""
        parts = rel.split("/")
        if len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in ("dialects", "parsers", "generators", "typing"):
            name = parts[2][:-3]
            if name in ("dialect", "base", "__init__", "python", "mysql_base", "_typing"):
                return None
            m = 0
            for d in ("dialects", "parsers", "generators", "typing"):
                r = f"sqlglot/{d}/{name}.py"
                if r in self._raw:
                    e = self.files[r]
                    m |= self.mask(e["use"])
            return m
        return None

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
        du = self.dialect_users(rel) if NARROW else None
        if du is not None:
            return m | du
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


def _literal(node):
    import ast

    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_literal(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and _literal(k) and _literal(v) for k, v in zip(node.keys, node.values))
    return False


_TREES = {}


def _tree(text):
    """ast.parse(text), parsed once per text and shared: no caller may modify the tree."""
    t = _TREES.get(text)
    if t is None:
        import ast

        if len(_TREES) >= 16:
            _TREES.clear()
        t = _TREES[text] = ast.parse(text)
    return t


def _same_no_doc(a, b):
    """True when two ASTs are equal once every module, class and function docstring is removed
    (a body holding only a docstring counts as `pass`): what comparing ast.dump of both trees with
    docstrings stripped gives, without building the dumps."""
    import ast

    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    pass_ = [ast.Pass()]

    def body(n):
        b = n.body
        if b:
            first = b[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                return b[1:] or pass_
        return b

    def same(x, y):
        if type(x) is not type(y):
            return False
        if isinstance(x, ast.AST):
            doc = isinstance(x, owners)
            for f in x._fields:
                if doc and f == "body":
                    u, v = body(x), body(y)
                else:
                    u, v = getattr(x, f, None), getattr(y, f, None)
                if not same(u, v):
                    return False
            return True
        if isinstance(x, list):
            return len(x) == len(y) and all(same(u, v) for u, v in zip(x, y))
        return x is y or repr(x) == repr(y)

    return same(a, b)


def import_time_effects(text):
    """(dumps of statements that run code at import, {new-assignment name: literal?}) found at
    module level or in class bodies, outside functions."""
    import ast

    flags, assigned = set(), {}

    def walk(body):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Import, ast.ImportFrom, ast.Pass)):
                continue
            if isinstance(st, ast.ClassDef):
                walk(st.body)
            elif isinstance(st, (ast.Assign, ast.AnnAssign)):
                targets = st.targets if isinstance(st, ast.Assign) else [st.target]
                lit = st.value is None or _literal(st.value)
                for t in targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            assigned[n.id] = assigned.get(n.id, True) and lit
            elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant):
                continue
            else:
                flags.add(ast.dump(st))
                for field in ("body", "orelse", "finalbody"):
                    sub = getattr(st, field, None)
                    if isinstance(sub, list):
                        walk(sub)

    walk(_tree(text).body)
    return flags, assigned


def _is_module(name):
    path = os.path.join(SRC, *name.split("."))
    return os.path.isfile(path + ".py") or os.path.isfile(os.path.join(path, "__init__.py"))


def _module_imports(text, rel):
    """Modules loaded by imports outside functions (module level, class bodies, if/try blocks).
    `from pkg import name` loads pkg, and pkg.name too when that is a module in the source tree."""
    import ast

    pkg = rel[:-3].replace("/", ".").rsplit(".", 1)[0] if rel.endswith(".py") else ""
    if rel.endswith("/__init__.py"):
        pkg = rel[: -len("/__init__.py")].replace("/", ".")
    out = set()

    def walk(body):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(st, ast.Import):
                out.update(a.name for a in st.names)
            elif isinstance(st, ast.ImportFrom):
                if st.level:
                    base = pkg.split(".")
                    base = base[: len(base) - (st.level - 1)] if st.level > 1 else base
                    mod = ".".join([*base, st.module] if st.module else base)
                else:
                    mod = st.module or ""
                out.add(mod)
                for a in st.names:
                    if _is_module(f"{mod}.{a.name}"):
                        out.add(f"{mod}.{a.name}")
            for field in ("body", "orelse", "finalbody"):
                sub = getattr(st, field, None)
                if isinstance(sub, list):
                    walk([x for x in sub if isinstance(x, ast.stmt)])
            for h in getattr(st, "handlers", None) or ():
                walk(h.body)

    walk(_tree(text).body)
    return out


def adds_new_import(source_old, new_text, rel=""):
    """A module-level import of a module this file did not import before, other than the standard
    library: loading it at import time can create an import cycle, register a dialect eagerly
    (test_lazy_load) or fail outright, so no coverage of the old code bounds it."""
    stdlib = getattr(sys, "stdlib_module_names", ())
    for m in _module_imports(new_text, rel) - _module_imports(source_old, rel):
        top = m.split(".", 1)[0]
        if top not in stdlib:
            return True
    return False


def _top_names(text):
    """Names bound at module level (defs, classes, assignments, imports), outside functions."""
    import ast

    out = set()

    def walk(body):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(st.name)
            elif isinstance(st, (ast.Import, ast.ImportFrom)):
                for a in st.names:
                    out.add((a.asname or a.name).split(".", 1)[0])
            elif isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                for tg in (st.targets if isinstance(st, ast.Assign) else [st.target]):
                    for n in ast.walk(tg):
                        if isinstance(n, ast.Name):
                            out.add(n.id)
            for field in ("body", "orelse", "finalbody"):
                sub = getattr(st, field, None)
                if isinstance(sub, list) and not isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    walk(sub)
            for h in getattr(st, "handlers", None) or ():
                walk(h.body)

    walk(_tree(text).body)
    return out


def _import_time_refs(text, names, module):
    """True when `text` imports one of `names` from `module`, star-imports `module`, or reads
    `.name` for one of them outside function bodies (code that runs while the file is imported)."""
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return True
    funcs = [(n.lineno, n.end_lineno) for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == module:
            if any(a.name == "*" or a.name in names for a in n.names):
                return True
        elif isinstance(n, ast.Attribute) and n.attr in names:
            ln = n.lineno
            if not any(a < ln <= b for a, b in funcs):
                return True
    return False


def removes_imported_name(rel, source_old, new_text):
    """An edit that stops a module from defining a top-level name another module imports by name
    (`from mod import name`) or reads while it is imported (`exp.Name` in a class body) makes that
    importer fail to import; coverage of the module itself never shows it."""
    if not rel.endswith(".py"):
        return False
    import re

    gone = _top_names(source_old) - _top_names(new_text)
    if not gone:
        return False
    module = rel[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    # A file can only match _import_time_refs if it names the module or reads `.name`.
    pat = re.compile(re.escape(module) + r"\b|\.\s*(" + "|".join(re.escape(n) for n in sorted(gone)) + r")\b")
    for top in ("sqlglot", "tests"):
        for dirpath, _, fnames in os.walk(os.path.join(SRC, top)):
            for fn in fnames:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                if os.path.relpath(path, SRC) == rel:
                    continue
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                if pat.search(text) and _import_time_refs(text, gone, module):
                    return True
    return False


def adds_import_time_code(source_old, new_text, rel=""):
    old_flags, old_assigned = import_time_effects(source_old)
    new_flags, new_assigned = import_time_effects(new_text)
    if new_flags - old_flags:
        return True
    if adds_new_import(source_old, new_text, rel):
        return True
    return any(n not in old_assigned and not lit for n, lit in new_assigned.items())


def _func_bodies(text):
    """[(first body line, last line)] of every function in `text`; None if it does not parse."""
    if text is None:
        return None
    import ast

    try:
        tree = _tree(text)
    except SyntaxError:
        return None
    return [(n.body[0].lineno, n.end_lineno) for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.body]


def _class_spans(text):
    """[(first line, last line, qualname)] of every class in `text`, with method bodies cut out:
    a line inside a class body but outside any function body belongs to the innermost class."""
    if text is None:
        return None
    import ast

    try:
        tree = _tree(text)
    except SyntaxError:
        return None
    out = []

    def visit(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.ClassDef):
                q = prefix + ch.name
                out.append((ch.lineno, ch.end_lineno, q))
                visit(ch, q + ".")
            elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            else:
                visit(ch, prefix)

    visit(tree, "")
    return out


def class_level_inst(mp, rel, lines, source_old):
    """Tests that instantiated the class (or a subclass) whose body holds one of `lines`."""
    if not mp.inst or not lines:
        return 0
    m = 0
    for q in class_level_owners(rel, lines, source_old):
        if rel + "|" + q in mp.impcls:
            return mp.all_mask()  # instantiated while sqlglot is imported: an import can now fail
        sid = mp.inst.get(rel + "|" + q)
        if sid is not None:
            m |= mp.mask(sid)
    return m


_owner_cache = {}


def class_level_owners(rel, lines, source_old):
    """Qualnames of the innermost classes whose own body (not a method: decorators, header and body
    of every function are excluded) holds one of `lines`."""
    key = (rel, len(source_old or ""))
    if key not in _owner_cache:
        import ast

        spans, funcs = _class_spans(source_old) or [], []
        try:
            for n in ast.walk(_tree(source_old or "")):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    first = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
                    funcs.append((first, n.end_lineno))
        except SyntaxError:
            pass
        _owner_cache[key] = (spans, funcs)
    spans, funcs = _owner_cache[key]
    out = set()
    for ln in lines:
        if not ln or any(a <= ln <= b for a, b in funcs):
            continue
        inner = [c for c in spans if c[0] <= ln <= c[1]]
        if inner:
            out.add(min(inner, key=lambda c: c[1] - c[0])[2])
    return out


EXPR_NARROW = os.environ.get("BR_EXPR_NARROW", "1") == "1"


_atl_cache = {}


def _arg_types_lines(rel, source_old):
    """Line numbers covered by `arg_types = ...` / `arg_types: ... = ...` statements directly in a
    class body."""
    key = (rel, len(source_old or ""))
    if key not in _atl_cache:
        import ast

        out = set()
        try:
            tree = _tree(source_old or "")
        except SyntaxError:
            tree = None
        for node in ast.walk(tree) if tree is not None else ():
            if isinstance(node, ast.ClassDef):
                for st in node.body:
                    targets = st.targets if isinstance(st, ast.Assign) else [st.target] if isinstance(st, ast.AnnAssign) else []
                    if targets and all(isinstance(t, ast.Name) and t.id == "arg_types" for t in targets):
                        out.update(range(st.lineno, st.end_lineno + 1))
        _atl_cache[key] = out
    return _atl_cache[key]


def expr_class_narrow(mp, rel, lines, source_old):
    """For a line of an `arg_types` assignment in the body of an expression class in
    sqlglot/expressions/ that tests instantiated: the tests that ran any line of that class. Its instantiators (and those
    of subclasses) are added by class_level_inst. Such attributes (arg_types, ...) reach behaviour
    only through instances; copies and unpickling go through __init__ too, so instantiation bounds
    them. None when this does not apply (keep the wider fallback)."""
    if not (EXPR_NARROW and INST and mp.inst and rel.startswith("sqlglot/expressions/")):
        return None
    owners = set()
    for ln in lines:
        if not ln or mp.in_function(rel, ln):
            return None
        if ln not in _arg_types_lines(rel, source_old):
            return None  # other class attributes (_sql_names, ...) are read at import by other code
        o = class_level_owners(rel, [ln], source_old)
        if len(o) != 1:
            return None
        owners |= o
    if len(owners) != 1:
        return None
    q = next(iter(owners))
    if rel + "|" + q in mp.impcls or mp.inst.get(rel + "|" + q) is None:
        return None
    m = 0
    e = mp.files[rel]
    for ln in lines:
        best = None
        for a, b, kind, sid in e["spans"]:
            if kind == "c" and a <= ln <= b and (best is None or b - a < best[1] - best[0]):
                best = (a, b, sid)
        if best is not None:
            m |= mp.mask(best[2])
    return m


_cstmt_cache = {}


def _class_stmts(rel, source_old):
    """[(first line, last line, class qualname, target names)] of statements directly in a class body."""
    key = (rel, len(source_old or ""))
    if key not in _cstmt_cache:
        import ast

        out = []

        def visit(node, prefix):
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, ast.ClassDef):
                    q = prefix + ch.name
                    for st in ch.body:
                        tg = []
                        if isinstance(st, ast.Assign):
                            tg = st.targets
                        elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
                            tg = [st.target]
                        names = [t.id for t in tg if isinstance(t, ast.Name)]
                        if names:
                            out.append((st.lineno, st.end_lineno, q, names))
                    visit(ch, q + ".")
                elif not isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    visit(ch, prefix)

        try:
            visit(_tree(source_old or ""), "")
        except SyntaxError:
            pass
        _cstmt_cache[key] = out
    return _cstmt_cache[key]


def import_read_attr(mp, rel, lines, source_old):
    """True when one of `lines` lies in a class attribute that code read while sqlglot was imported,
    outside the creation of that class and its subclasses (impattrs): e.g. SQL templates parsed at
    import read Dialect.NULL_ORDERING and keep the result in module state no test reads it from."""
    if not mp.impattrs or all(not ln or mp.in_function(rel, ln) for ln in lines):
        return False
    for a, b, q, names in _class_stmts(rel, source_old):
        if any(ln and a <= ln <= b for ln in lines) and any(rel + "|" + q + "|" + n in mp.impattrs for n in names):
            return True
    return False


PICKLE_FUNCS = ("__reduce__", "__reduce_ex__", "__getstate__", "__setstate__", "__getnewargs__",
                "__getnewargs_ex__")


def _pickle_lines(rel, source_old):
    """Lines of functions that pickling runs (serde.py, __reduce__ and friends)."""
    import ast

    if rel == "sqlglot/serde.py":
        return None  # the whole module
    out = []
    if not any(name in (source_old or "") for name in PICKLE_FUNCS):
        return out
    try:
        for n in ast.walk(_tree(source_old or "")):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in PICKLE_FUNCS:
                first = min([n.lineno] + [d.lineno for d in n.decorator_list])
                out.append((first, n.end_lineno))
    except SyntaxError:
        return None
    return out


_POOL = []


def pool_mask(mp):
    """Tests that sent work to a process pool (a `.submit(` line in a test module). The build-time
    tracer records a worker's coverage only while the submitted function runs, not while the worker
    unpickles its arguments or pickles its result (serde.load / Expr.__reduce__ / serde.dump), so
    those lines miss these tests."""
    if _POOL:
        return _POOL[0]
    m = 0
    for rel in sorted(mp.test_files):
        if rel not in mp.files:
            continue
        try:
            with open(os.path.join(SRC, rel), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if ".submit(" not in text:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if ".submit(" in line:
                m |= mp.line_mask(rel, i)
    _POOL.append(m)
    return m


def pickle_path_mask(mp, rel, f, source_old):
    if not rel.startswith("sqlglot/"):
        return 0
    removed, inserts = changed_lines(f)
    touched = list(removed) + [x for p in inserts for x in p]
    pl = _pickle_lines(rel, source_old)
    if pl is None and rel != "sqlglot/serde.py":
        return 0
    if pl is None or any(a <= ln <= b for ln in touched for a, b in pl):
        return pool_mask(mp)
    added = [c for _, ls in f["hunks"] for t, c in ls if t == "+"]
    if any(name in c for c in added for name in PICKLE_FUNCS):
        return pool_mask(mp)  # a new pickling hook
    return 0


_INTRO = {}


def introspect_mask(mp, rel):
    """Test modules that load every module of rel's package themselves (importlib / pkgutil) and
    compare their tables or read their source (test_typing.py checks sqlglot/typing/*.py for
    redundant EXPRESSION_METADATA entries). Neither coverage nor the data-file map sees that: the
    modules are already imported and .py reads are not recorded."""
    if not rel.startswith("sqlglot/") or not rel.endswith(".py"):
        return 0
    pkg = ".".join(rel[:-3].split("/")[:-1])
    if pkg in _INTRO:
        return _INTRO[pkg]
    import re

    if None not in _INTRO:  # test modules that load modules dynamically, read once
        dyn = []
        for t in sorted(mp.test_files):
            try:
                with open(os.path.join(SRC, t), encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            if "importlib" in text or "pkgutil" in text:
                dyn.append((t, text))
        _INTRO[None] = dyn
    pat = re.compile(re.escape(pkg) + r"(?![\w])")
    m = 0
    for t, text in _INTRO[None]:
        if pat.search(text):
            m |= mp.mask(mp.test_files[t])
    _INTRO[pkg] = m
    return m


_CACHE = {}


def _cached(key, fn):
    """Per-run cache for facts about the pinned (old) source of a file."""
    if key not in _CACHE:
        _CACHE[key] = fn()
    return _CACHE[key]


_MODULE_DUNDERS = {"__name__", "__file__", "__doc__", "__spec__", "__package__", "__loader__", "__path__",
                   "__builtins__", "__annotations__", "__dict__", "__qualname__", "__module__", "__class__"}


def _bindings(body):
    """[(line, name)] bound by statements in `body` (imports, defs, classes, assignments), recursing
    into if/try/with/for blocks but not into defs or classes. "*" marks a star import."""
    import ast

    out = []
    for st in body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append((st.lineno, st.name))
            continue
        if isinstance(st, (ast.Import, ast.ImportFrom)):
            for a in st.names:
                out.append((st.lineno, "*" if a.name == "*" else (a.asname or a.name).split(".", 1)[0]))
        else:
            for n in ast.walk(st):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    out.append((st.lineno, n.id))
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    break
        for field in ("body", "orelse", "finalbody"):
            sub = getattr(st, field, None)
            if isinstance(sub, list) and not isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.extend(_bindings(sub))
        for h in getattr(st, "handlers", None) or ():
            if getattr(h, "name", None):
                out.append((h.lineno, h.name))
            out.extend(_bindings(h.body))
    return out


def def_time_unbound(text):
    """{(def qualname, name)}: names that a module- or class-level def's decorators, default values or
    evaluated annotations use but that nothing binds before the def (builtins aside). Those are
    evaluated when the def statement runs, i.e. at import: an unbound one raises NameError there."""
    import ast
    import builtins

    tree = _tree(text)
    lazy_ann = any(isinstance(st, ast.ImportFrom) and st.module == "__future__" and
                   any(a.name == "annotations" for a in st.names) for st in tree.body)
    known = set(dir(builtins)) | _MODULE_DUNDERS
    mod_binds = _bindings(tree.body)
    if any(n == "*" for _, n in mod_binds):
        return set()
    out = set()

    def exprs(fn):
        a = fn.args
        es = list(fn.decorator_list) + list(a.defaults) + [d for d in a.kw_defaults if d is not None]
        if not lazy_ann:
            for arg in a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x]:
                if arg.annotation is not None:
                    es.append(arg.annotation)
            if fn.returns is not None:
                es.append(fn.returns)
        return es

    def names(e):
        stack = [e]
        while stack:
            n = stack.pop()
            if isinstance(n, ast.Lambda):
                stack.extend(n.args.defaults)
                continue
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                yield n.id
            stack.extend(ast.iter_child_nodes(n))

    def visit(body, prefix, scope_binds, class_line):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                line = min([st.lineno] + [d.lineno for d in st.decorator_list])
                bound = {n for ln, n in scope_binds if ln < line}
                if class_line is not None:
                    bound |= {n for ln, n in mod_binds if ln < class_line}
                for e in exprs(st):
                    for nm in names(e):
                        if nm not in bound and nm not in known:
                            out.add((prefix + st.name, nm))
            elif isinstance(st, ast.ClassDef):
                visit(st.body, prefix + st.name + ".", _bindings(st.body),
                      st.lineno if class_line is None else class_line)
            else:
                for field in ("body", "orelse", "finalbody"):
                    sub = getattr(st, field, None)
                    if isinstance(sub, list):
                        visit(sub, prefix, scope_binds, class_line)
                for h in getattr(st, "handlers", None) or ():
                    visit(h.body, prefix, scope_binds, class_line)

    visit(tree.body, "", mod_binds, None)
    return out


def _literal_only(f):
    """True when the removed and added lines differ only in literal tokens (strings, numbers,
    True/False/None) and comments."""
    import io
    import tokenize

    skip = {tokenize.STRING, tokenize.NUMBER, tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
            tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}

    def toks(lines):
        out = []
        try:
            src = "".join(l.strip() + "\n" for l in lines)
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type in skip or (tok.type == tokenize.NAME and tok.string in ("True", "False", "None")):
                    continue
                out.append(tok.string)
        except (tokenize.TokenError, IndentationError, SyntaxError):
            return None
        return out

    groups = _replacement_groups(f)
    if not groups:
        return False
    for olds, news in groups:
        a, b = toks(olds), toks(news)
        if a is None or a != b:
            return False
    return True


_DEFINED = []


def _defined_names():
    """Every name the pinned sqlglot sources define (classes, functions, assignments, enum members)."""
    if not _DEFINED:
        import re

        pat = re.compile(r"^\s*(?:class|def|async def)\s+(\w+)|^\s*(\w+)\s*(?::[^=\n]*)?=(?!=)", re.M)
        names = set()
        for dirpath, _, fnames in os.walk(os.path.join(SRC, "sqlglot")):
            for fn in fnames:
                if fn.endswith(".py"):
                    try:
                        with open(os.path.join(dirpath, fn), encoding="utf-8", errors="replace") as fh:
                            for a, b in pat.findall(fh.read()):
                                names.add(a or b)
                    except OSError:
                        pass
        _DEFINED.append(names)
    return _DEFINED[0]


def references_undefined(source_old, new_text, f):
    """An added line reads `alias.Name` where alias is imported from sqlglot (exp, TokenType, ...)
    and nothing in the pinned sources defines Name: in a class body or at module level that raises
    AttributeError when the module is imported (exp.ArrayUniq in a TRANSFORMS dict)."""
    import ast

    added = _added_new_lines(f)
    if not added:
        return False
    tree = _tree(new_text)
    aliases = set()
    for st in ast.walk(tree):
        if isinstance(st, ast.ImportFrom) and (st.module or "").startswith("sqlglot"):
            aliases.update(a.asname or a.name for a in st.names)
        elif isinstance(st, ast.Import):
            aliases.update((a.asname or a.name).split(".", 1)[0] for a in st.names if a.name.startswith("sqlglot"))
    defined = None
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and getattr(n, "lineno", None) in added and isinstance(n.value, ast.Name) \
                and n.value.id in aliases:
            if defined is None:
                defined = _defined_names()
            if n.attr not in defined:
                return True
    return False


def _replacement_groups(f):
    """[(removed lines, added lines)] per contiguous change; None when some change is a pure
    removal or a pure addition (a moved or new statement is never "literal-only")."""
    out = []
    for _, lines in f["hunks"]:
        olds, news = [], []
        for tag, c in lines + [(" ", "")]:
            if tag == "-":
                olds.append(c)
            elif tag == "+":
                news.append(c)
            elif olds or news:
                if not olds or not news:
                    return None
                out.append((olds, news))
                olds, news = [], []
    return out


def _scalar_literal_only(f):
    """The removed and added lines differ only in number tokens and True/False/None."""
    import io
    import tokenize

    def toks(lines):
        out = []
        try:
            src = "".join(l.strip() + "\n" for l in lines)
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type in (tokenize.NUMBER, tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                                tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER):
                    continue
                if tok.type == tokenize.NAME and tok.string in ("True", "False", "None"):
                    continue
                out.append(tok.string)
        except (tokenize.TokenError, IndentationError, SyntaxError):
            return None
        return out

    groups = _replacement_groups(f)
    if not groups:
        return False
    for olds, news in groups:
        a, b = toks(olds), toks(news)
        if a is None or a != b:
            return False
    return True


def class_level_code_change(mp, f, source_old=None, new_text=None):
    """A change outside every function body that can break the module's import: it reads an
    attribute of a sqlglot import that the pinned sources never define."""
    key = f["old"]
    if key not in mp.files or not key.endswith(".py"):
        return True
    removed, inserts = changed_lines(f)
    outside = any(not mp.in_function(key, ln) for ln in removed) or any(
        not (mp.in_function(key, max(a, 1)) and mp.in_function(key, max(b, 1))) for a, b in inserts)
    if not outside or _literal_only(f):
        return False
    if source_old is None:
        try:
            with open(os.path.join(SRC, key), encoding="utf-8") as fh:
                source_old = fh.read()
            new_text = apply_patch(source_old, f)
        except Exception:
            return True
    try:
        return references_undefined(source_old, new_text, f)
    except Exception:
        return True


_MUTATORS = {"add", "update", "append", "extend", "insert", "setdefault", "pop", "popitem", "remove",
             "discard", "clear", "intersection_update", "difference_update", "symmetric_difference_update"}


def _added_new_lines(f):
    """Line numbers, in the new file, of the patch's added lines."""
    out = set()
    delta = 0
    for start, lines in f["hunks"]:
        new = start + delta
        for tag, _ in lines:
            if tag == "+":
                out.add(new)
                new += 1
                delta += 1
            elif tag == "-":
                delta -= 1
            else:
                new += 1
    return out


_MUT_RE = None


def mutates_shared_state(source_old, new_text, f):
    """An added line, inside a function, that mutates in place a module- or class-level container:
    `X |= ...`, `X.add(...)`, `X[k] = ...` where X is a module-level name, an attribute reached from
    one (exp.DataType.TEMPORAL_TYPES) or from `cls`, or a local alias of such a value. Later tests in
    the same process see the change, so coverage of the edited lines does not bound it."""
    import ast

    global _MUT_RE
    import re

    if _MUT_RE is None:
        _MUT_RE = re.compile(r"(\|=|&=|\^=|-=|\+=|\]\s*=[^=]|\.(?:" + "|".join(sorted(_MUTATORS)) + r")\s*\()")
    if not any(_MUT_RE.search(c) for _, ls in f["hunks"] for t, c in ls if t == "+"):
        return False
    added = _added_new_lines(f)
    if not added:
        return False
    tree = _tree(new_text)
    mod = {n for _, n in _bindings(tree.body)}

    def rooted(e, aliases):
        while isinstance(e, (ast.Attribute, ast.Subscript)):
            e = e.value
        return isinstance(e, ast.Name) and (e.id in mod or e.id in aliases or e.id == "cls")

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(fn.lineno <= ln <= fn.end_lineno for ln in added):
            continue
        aliases = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                v = n.value
                if isinstance(v, ast.Attribute) and rooted(v, set()):
                    aliases.add(n.targets[0].id)
        local = {a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs}
        for n in ast.walk(fn):
            ln = getattr(n, "lineno", None)
            if ln not in added:
                continue
            tgt = None
            if isinstance(n, ast.AugAssign):
                tgt = n.target
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _MUTATORS:
                tgt = n.func.value
            elif isinstance(n, ast.Assign):
                for tg in n.targets:
                    if isinstance(tg, ast.Subscript):
                        tgt = tg.value
            if tgt is None:
                continue
            if isinstance(tgt, ast.Name):
                if tgt.id in aliases or (tgt.id in mod and tgt.id not in local and isinstance(n, (ast.Call, ast.Assign))):
                    return True
            elif isinstance(tgt, (ast.Attribute, ast.Subscript)):
                base = tgt
                while isinstance(base, (ast.Attribute, ast.Subscript)):
                    base = base.value
                if isinstance(base, ast.Name) and base.id != "self" and base.id not in local - {"cls"} and (
                        base.id in mod or base.id in aliases or base.id == "cls"):
                    return True
    return False


def _memo_index(source_old):
    """(functions [(start, end, name, cached)], callers {name: [(call line, memoising caller)]})."""
    import ast

    tree = _tree(source_old)
    mod = {n for _, n in _bindings(tree.body)}
    funcs, callers = [], {}
    for g in ast.walk(tree):
        if not isinstance(g, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        cached = any((isinstance(d, ast.Name) and d.id in ("cache", "lru_cache")) or
                     (isinstance(d, ast.Attribute) and d.attr in ("cache", "lru_cache")) or
                     (isinstance(d, ast.Call) and getattr(d.func, "attr", getattr(d.func, "id", "")) in ("cache", "lru_cache"))
                     for d in g.decorator_list)
        funcs.append((g.lineno, g.end_lineno, g.name, cached))
        stores = False
        calls = []
        for n in ast.walk(g):
            if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store) and isinstance(n.value, ast.Name) \
                    and n.value.id in mod:
                stores = True
            elif isinstance(n, ast.Call):
                name = n.func.id if isinstance(n.func, ast.Name) else n.func.attr if isinstance(n.func, ast.Attribute) else None
                if name:
                    calls.append((name, n.lineno))
        for name, ln in calls:
            if name != g.name:
                callers.setdefault(name, []).append((ln, stores))
    return funcs, callers


def memo_caller_mask(mp, rel, lines, source_old):
    """Callers' spans for an edited function whose result is memoised: called from a function that
    stores into a module-level container (`_DISPATCH_CACHE[cls] = _build_dispatch(cls)`) or decorated
    with lru_cache/cache. Its body then runs once per process, so the map credits it only to the
    tests that happened to fill the cache."""
    funcs, callers = _cached(("memo", rel), lambda: _memo_index(source_old))
    m = 0
    for a, b, name, cached in funcs:
        if any(a <= ln <= b for ln in lines):
            for ln, stores in callers.get(name, ()):
                if cached or stores:
                    m |= mp.span_mask(rel, ln)
    return m


def _lambda_body_edits(f, removed):
    """Old line numbers whose replacement keeps the line identical up to its `lambda`, so the edit is
    confined to the lambda (its parameters or body), which the line's coverage does bound."""
    out = set()
    olds, news = [], []
    for _, lines in f["hunks"]:
        for tag, c in lines + [(" ", "")]:
            if tag == "-":
                olds.append(c)
            elif tag == "+":
                news.append(c)
            else:
                if len(olds) == len(news):
                    for o, n in zip(olds, news):
                        if "lambda" in o and "lambda" in n and o[: o.index("lambda")] == n[: n.index("lambda")]:
                            out.add(o)
                olds, news = [], []
    texts = {}
    i = 0
    for _, lines in f["hunks"]:
        for tag, c in lines:
            if tag == "-":
                if i < len(removed):
                    texts[removed[i]] = c
                i += 1
    return {ln for ln, c in texts.items() if c in out}


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
            # every real patch is against the pinned tree: not applying means we misread it
            return import_breaking_mask(mp, rel)
        if new_text is not None:
            try:
                # Full compile: symtable/codegen errors (duplicate argument, misplaced __future__
                # import, async comprehension outside async def) are not raised by ast.parse.
                compile(new_text, rel, "exec", 0)
            except Exception:
                return import_breaking_mask(mp, rel)  # does not even compile
            try:
                removed0, inserts0 = changed_lines(f)
                outside = any(not mp.in_function(rel, ln) for ln in removed0) or any(
                    not (mp.in_function(rel, max(a, 1)) and mp.in_function(rel, max(b, 1))) for a, b in inserts0)
                if outside and adds_import_time_code(source_old, new_text, rel):
                    return import_breaking_mask(mp, rel)  # new code that runs when the module is imported
                if def_time_unbound(new_text) - _cached(("dtu", rel), lambda: def_time_unbound(source_old)):
                    return import_breaking_mask(mp, rel)  # a def's decorator/default/annotation is unbound
                if rel.startswith("sqlglot/") and mutates_shared_state(source_old, new_text, f):
                    return mp.all_mask()  # shared state mutated in place: later tests in a process see it
                if removes_imported_name(rel, source_old, new_text):
                    return import_breaking_mask(mp, rel)  # an importer of a removed name fails to import
            except SyntaxError:
                return import_breaking_mask(mp, rel)
            except Exception:
                pass
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
                if _same_no_doc(_tree(new_text), _tree(source_old)):
                    return 0  # docstrings (and layout) only: no test reads them, doctests are not scored
            except Exception:
                pass
    removed, inserts = changed_lines(f)
    m = 0
    # Test modules are imported during collection, so their import-time lines (module and class
    # level, def headers with default values, decorators) are credited to whichever collector ran
    # first. Treat such lines as reaching every test that touched or imports the file.
    tbody = None
    if rel.startswith("tests/"):
        tbody, twide = _func_bodies(source_old), mp.mask(mp.files[rel]["all"]) | mp.mask(mp.files[rel]["cuse"])
        if rel in mp.test_files:
            twide |= mp.mask(mp.test_files[rel])
        if tbody is None:
            return twide | import_breaking_mask(mp, rel) if source_old is None else twide

        def in_body(n):
            return any(a <= n <= b for a, b in tbody)

        if any(not in_body(ln) for ln in removed) or any(not (in_body(p) and in_body(q)) for p, q in inserts):
            m |= twide
    # Function bodies that run while sqlglot is imported (metaclasses, __init_subclass__, table
    # builders) shape module state every test sees, but the map credits them only to the few
    # tests that run them again later. When such a function is not run by most tests at test
    # time, the map cannot bound an edit to it, so every test is selected.
    imp = mp.files[rel].get("imp") if rel.startswith("sqlglot/") else None
    if imp:
        def at_import(n):
            return any(a <= n <= b for a, b in imp)

        for ln in list(removed) + [x for p in inserts for x in p]:
            if ln and at_import(ln) and bin(mp.span_mask(rel, ln)).count("1") <= IMP_MAX:
                # every test: some (test_lazy_load) import sqlglot in a subprocess no map can see
                return mp.all_mask()
    if rel.startswith("sqlglot/") and import_read_attr(mp, rel, list(removed) + [x for p in inserts for x in p],
                                                       source_old):
        return mp.all_mask()
    if rel.startswith("sqlglot/") and source_old is not None:
        try:
            m |= memo_caller_mask(mp, rel, [ln for ln in list(removed) + [x for p in inserts for x in p] if ln],
                                  source_old)
        except Exception:
            pass
    if rel.startswith("sqlglot/") and INST:
        # class attributes (arg_types, ...) are consumed when the class is created: every test that
        # instantiated the class or a subclass can see the change
        m |= class_level_inst(mp, rel, list(removed) + [x for p in inserts for x in p], source_old)
    lambda_only = _lambda_body_edits(f, removed)
    for ln in removed:
        lm = mp.line_mask(rel, ln)
        if not lm and mp.is_dead(rel, ln) and not any(a <= ln <= b for a, b in mp.files[rel]["strs"]):
            if tbody is not None:
                m |= twide  # test modules: code only a pool worker runs (initializer) is never traced
            continue  # never executed by any test (and not docstring text)
        if not mp.in_function(rel, ln):
            am = mp.attr_mask(rel, ln)
            if am is not None:
                m |= am | lm  # precise: tests that read this class attribute (plus lambda-entry hits)
                continue
        if lm and mp.in_function(rel, ln):
            m |= lm
        elif lm:
            m |= lm | mp.stmt_refs(rel, ln)  # class-level line that also runs in tests (lambda entries)
            if ln not in lambda_only:
                m |= mp.span_mask(rel, ln)  # the edit is outside the lambda body (its key, its target)
        else:
            nm = expr_class_narrow(mp, rel, [ln], source_old)
            m |= mp.span_mask(rel, ln) if nm is None else nm
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
            nm = expr_class_narrow(mp, rel, [max(prev, 1), max(nxt, 1)], source_old)
            if nm is None:
                m |= mp.nonfunc_mask(rel, max(prev, 1)) | mp.nonfunc_mask(rel, max(nxt, 1))
            else:
                m |= nm
    # not gated on m: a line only a pool worker runs is recorded under no test, so it looks dead
    return m | pickle_path_mask(mp, rel, f, source_old) | (introspect_mask(mp, rel) if m else 0)


NARROW = True
IMP_MAX = int(os.environ.get("BR_IMP_MAX", "1000000"))  # import-time code: always every test
INST = os.environ.get("BR_INST", "1") == "1"

GIANTS = {
    # id -> (dirs whose edits cannot plausibly change it, files within those dirs that still can)
    "tests/test_generator.py::TestGenerator::test_generator_memory": (
        ("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/typing/", "sqlglot/optimizer/", "sqlglot/executor/"),
        ("sqlglot/dialects/dialect.py", "sqlglot/dialects/__init__.py", "sqlglot/parsers/base.py",
         "sqlglot/parsers/__init__.py", "sqlglot/generators/base.py", "sqlglot/generators/__init__.py"),
    ),
    "tests/test_parser.py::TestParser::test_max_nodes": (
        ("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/typing/",
         "sqlglot/optimizer/", "sqlglot/executor/"),
        ("sqlglot/dialects/dialect.py", "sqlglot/dialects/__init__.py", "sqlglot/parsers/base.py",
         "sqlglot/parsers/__init__.py", "sqlglot/generators/base.py", "sqlglot/generators/__init__.py"),
    ),
}


def prune_giants(mp, files, m):
    """Drop the two slowest tests that only core files can break, when only peripheral files changed."""
    paths = [(f["old"], f["new"]) for f in files]
    for tid, (dirs, core) in GIANTS.items():
        try:
            bit = mp.tests.index(tid)
        except ValueError:
            continue
        ok = True
        for old, new in paths:
            for p in (old, new):
                if p.startswith("tests/") and p.endswith(".py"):
                    ok = False
                elif p.endswith(".py") and (not p.startswith(dirs) or p in core):
                    ok = False
                elif not p.endswith(".py") and not p.startswith("tests/fixtures/"):
                    ok = False
        if ok and paths and tid.endswith("test_generator_memory") and any(
                class_level_code_change(mp, f) for f in files):
            ok = False  # it instantiates every dialect: a class-body edit can break one at import
        if ok and paths:
            direct = 0
            for f in files:
                key = f["old"]
                if key in mp and key.endswith(".py"):
                    removed, inserts = changed_lines(f)
                    for ln in removed:
                        direct |= mp.line_mask(key, ln)
                    for a, b in inserts:
                        direct |= mp.line_mask(key, max(a, 1)) | mp.line_mask(key, max(b, 1))
            if not (direct >> bit) & 1:
                m &= ~(1 << bit)
    return m


def select(mp, files, extras):
    m = 0
    wide = 0  # files whose full coverage is offered as free filler
    for f in files:
        rel = f["new"] if not f["deleted"] else f["old"]
        old = f["old"]
        if rel in ALWAYS_ALL or old in ALWAYS_ALL or "conftest.py" in (rel.rsplit("/", 1)[-1], old.rsplit("/", 1)[-1]):
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
            names = ("tests/test_dialect_imports.py", "tests/test_dialect_entry_points.py")
            if rel.startswith("sqlglot/typing/"):  # test_typing imports every module in the package
                names += ("tests/test_typing.py",)
            for name in names:
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


# ---- runtime verification -------------------------------------------------------------------
# Run (part of) the static selection against the patched source and keep only what fails.
VERIFY = os.environ.get("BR_VERIFY", "0") == "1"
PROBE = os.environ.get("BR_PROBE", "")  # diagnostic submissions only; empty in normal use
GATE = os.environ.get("BR_GATE", "0") == "1"  # off: t6x013 fails only in the giant
SMALL_MS = int(os.environ.get("BR_SMALL_MS", "1500"))   # verify a whole selection up to this cost
SMALL_N = 40
GATE_MS = int(os.environ.get("BR_GATE_MS", "4000"))     # budget for the cheap part when a giant is present
SENT_MS = int(os.environ.get("BR_SENT_MS", "0"))        # sentinel budget when the cheap part is too big
GATED = {"tests/test_executor.py::TestExecutor::test_execute_tpcds"}
GATED_FILE = "tests/test_executor.py"
HEAVY = ("tests/test_optimizer.py", "tests/test_executor.py")  # expensive per-run setup
WALL_BUDGET = float(os.environ.get("BR_WALL", "90"))
_T0 = time.monotonic()
_verifier = None


def _get_verifier():
    global _verifier, VERIFY
    if _verifier is None:
        try:
            sys.path.insert(0, APP)
            from verify import Verifier

            v = Verifier(SRC, timeout_s=5)
            v.start()
        except Exception:
            VERIFY = False  # no test environment in this image: static answers only
            raise
        _verifier = v
    return _verifier


def _patched_files(files):
    """{rel: new text} for a patch we can reproduce exactly; None when we cannot."""
    out = {}
    for f in files:
        rel, old = f["new"], f["old"]
        if f["deleted"] or f["binary"] or rel != old or not (rel.startswith("sqlglot/") or rel.startswith("tests/")):
            return None
        base = os.path.join(SRC, old)
        if f["new_file"]:
            if os.path.exists(base):
                return None
            text = ""
        else:
            if not os.path.exists(base):
                return None
            with open(base, encoding="utf-8") as fh:
                text = fh.read()
        out[rel] = apply_patch(text, f)
    return out


def _expand(entries, ids):
    out = set()
    for e in entries:
        for n in ids:
            k = ids[n]
            if k == e or k.startswith(e + "::") or k.startswith(e + "["):
                out.add(n)
    return out


def verify_commit(stem, files, chosen, ms, idof):
    """Return a refined list of test numbers, or None to keep the static selection."""
    if time.monotonic() - _T0 > WALL_BUDGET:
        return None
    giants = [n for n in chosen if idof.get(n) in GATED] if GATE else []
    rest = [n for n in chosen if n not in giants]
    cost = sum(ms[n] for n in rest)
    sentinel = False
    if giants:
        if not rest:
            return None
        stop_first = True
        if cost > GATE_MS:
            if SENT_MS <= 0:
                return None
            # too much to run: run the cheapest part; if it all passes, drop only the giant
            sentinel, acc, part = True, 0, []
            for n in sorted((n for n in rest if not idof[n].startswith(HEAVY)), key=lambda n: ms[n]):
                if acc + ms[n] > SENT_MS:
                    break
                part.append(n)
                acc += ms[n]
            # the giant's own test file exercises the same code path: its cheap tests always run
            same = [n for n in rest if idof[n].startswith(GATED_FILE) and ms[n] < 2000]
            if not same:
                return None
            part += same
            acc += sum(ms[n] for n in same)
            done = set(part)
            keep, rest, cost = [n for n in rest if n not in done], part, acc
    else:
        if cost > SMALL_MS or len(rest) > SMALL_N or any(idof[n].startswith(HEAVY) for n in rest):
            return None
        stop_first = False
    pf = _patched_files(files)
    if pf is None:
        return None
    run = sorted(rest, key=lambda n: (idof[n].startswith(HEAVY), ms[n]))
    failing, ok, _cpu = _get_verifier().run(pf, [idof[n] for n in run], expected_seconds=cost / 1000,
                                           stop_first=stop_first)
    if os.environ.get("BR_DEBUG"):
        print(f"{stem}: verify n={len(run)} rec={cost}ms cpu={_cpu:.2f} ok={ok} failing={len(failing)}", file=sys.stderr)
    if not ok:
        return None
    sub = {n: idof[n] for n in run}
    bad = _expand(failing, sub)
    if failing and not bad:  # a failure we cannot attribute (collection error elsewhere)
        return None
    if giants:
        if bad:
            return None
        return sorted(keep) if sentinel else []
    return sorted(bad)


# ---- import probe ---------------------------------------------------------------------------
# A line that runs while a module is imported (module level, class bodies, decorators, def/class
# headers) can make that import raise: a typo in an attribute, a SQL template that no longer
# parses, a name used before it is bound, a name imported from the wrong module. No map can bound
# that, so import every sqlglot module from a copy of the tree with the patch applied, in a child
# process, and compare the modules that fail with the unpatched tree.
IMPORT_PROBE = os.environ.get("BR_IMPORT_PROBE", "1") == "1"
_PROBE_CODE = r"""
import importlib, pkgutil, sys
sys.path.insert(0, sys.argv[1])
import sqlglot
from sqlglot.dialects.dialect import Dialect
bad = []
if len(Dialect._classes) != 1:
    bad.append("<lazy>")
for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if info.name == "sqlglot.__main__":
        continue
    try:
        importlib.import_module(info.name)
    except BaseException:
        bad.append(info.name)
print("\n".join(sorted(bad)))
print("<END>")
"""
_PROBE_BASE = []
_probe_seq = [0]


def _run_probe(root):
    """Modules that fail to import from `root` (plus "<lazy>"), or None if the probe itself failed."""
    import subprocess

    env = {"PATH": os.environ.get("PATH", ""), "HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONHASHSEED": "0"}
    try:
        p = subprocess.run([sys.executable, "-I", "-S", "-c", _PROBE_CODE, root], capture_output=True, text=True,
                           timeout=60, env=env, cwd="/tmp")
    except Exception:
        return None
    lines = p.stdout.splitlines()
    if p.returncode != 0 or not lines or lines[-1] != "<END>":
        return None
    return {l for l in lines[:-1] if l}


def _overlay(patched):
    """A copy of sqlglot/ in /tmp: symlinks to the pinned tree (bytecode included) with the patched
    files written as real files."""
    import shutil
    import tempfile

    _probe_seq[0] += 1
    ov = tempfile.mkdtemp(prefix=f"brp{_probe_seq[0]}_", dir="/tmp")
    real = {"sqlglot"}
    for rel in patched:
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            real.add("/".join(parts[:i]))

    def mirror(rd):
        os.makedirs(os.path.join(ov, rd), exist_ok=True)
        for name in os.listdir(os.path.join(SRC, rd)):
            rel = f"{rd}/{name}"
            if rel in patched:
                continue
            src = os.path.join(SRC, rel)
            if rel in real and os.path.isdir(src):
                mirror(rel)
            else:
                os.symlink(src, os.path.join(ov, rel))

    try:
        mirror("sqlglot")
        for rel, text in patched.items():
            if text is None:
                continue
            path = os.path.join(ov, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
    except Exception:
        shutil.rmtree(ov, ignore_errors=True)
        raise
    return ov


def import_time_files(files):
    """The commit's sqlglot .py files whose patch touches a line outside every function body."""
    out = []
    for f in files:
        rel = f["new"]
        if f["binary"] or not rel.startswith("sqlglot/") or not rel.endswith(".py"):
            continue
        if f["new_file"] or f["deleted"] or f["old"] != rel:
            out.append(f)
            continue
        try:
            with open(os.path.join(SRC, f["old"]), encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        try:
            new_text = apply_patch(text, f)
            if code_tokens(new_text) == code_tokens(text) or _same_no_doc(_tree(new_text), _tree(text)):
                continue  # comments or docstrings only: imports cannot change
            if _scalar_literal_only(f):
                continue  # only numbers / True / False / None changed: nothing new can raise
        except Exception:
            pass
        bodies = _cached(("bodies", rel), lambda: _func_bodies(text)) or []

        def inb(n):
            return any(a <= n <= b for a, b in bodies)

        removed, inserts = changed_lines(f)
        if any(not inb(ln) for ln in removed) or any(not (inb(max(a, 1)) and inb(max(b, 1))) for a, b in inserts):
            out.append(f)
    return out


def import_probe_breaks(files):
    """True when applying the commit makes some sqlglot module fail to import (or loads a dialect
    eagerly); False when imports behave as on the pinned tree or the probe cannot run."""
    import shutil

    patched = {}
    for f in files:
        rel = f["new"]
        if not rel.startswith("sqlglot/") or not rel.endswith(".py") or f["binary"]:
            continue
        if f["deleted"] or f["old"] != rel:
            return False  # handled by the static rules (all tests)
        base = os.path.join(SRC, f["old"])
        old = ""
        if not f["new_file"]:
            try:
                with open(base, encoding="utf-8") as fh:
                    old = fh.read()
            except OSError:
                return False
        try:
            patched[rel] = apply_patch(old, f)
        except Exception:
            return False
    if not patched:
        return False
    if not _PROBE_BASE:
        _PROBE_BASE.append(_run_probe(SRC))
    base = _PROBE_BASE[0]
    if base is None:
        return False
    ov = _overlay(patched)
    try:
        got = _run_probe(ov)
    finally:
        shutil.rmtree(ov, ignore_errors=True)
    return got is None or bool(got - base)


def main():
    with open(os.path.join(DATA, "tests.json"), encoding="utf-8") as fh:
        tests = json.load(fh)["tests"]
    num = {t["id"]: t["number"] for t in tests}
    idof = {t["number"]: t["id"] for t in tests}
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
            if m != mp.all_mask():
                m = prune_giants(mp, files, m)
            if IMPORT_PROBE and m != mp.all_mask() and import_time_files(files):
                try:
                    if import_probe_breaks(files):
                        m = mp.all_mask()
                except Exception as e:
                    print(f"{stem}: probe {e!r}", file=sys.stderr)
        except Exception as e:  # never crash: fall back to everything
            print(f"{stem}: {e!r}", file=sys.stderr)
            m, wide = mp.all_mask(), 0
        chosen = [number_of[i] for i in bits(m) if number_of[i] is not None]
        if m and unknown:
            chosen += unknown
        filler = [number_of[i] for i in bits(wide & ~m) if number_of[i] is not None]
        if PROBE == "empty":  # diagnostic: measures how many commits fail
            chosen, filler = [], []
        elif PROBE == "nogiants":  # diagnostic: measures what tests of 2 s or more cost
            chosen = [n for n in chosen if ms[n] < 2000]
            filler = [n for n in filler if ms[n] < 2000]
        if VERIFY and chosen:
            try:
                out = verify_commit(stem, files, chosen, ms, idof)
            except Exception as e:  # any verifier problem: keep the static answer
                print(f"{stem}: verify {e!r}", file=sys.stderr)
                out = None
            if out is not None:
                chosen, filler = out, []
        workers = plan([(n, ms[n]) for n in chosen], [(n, ms[n]) for n in filler])
        print(json.dumps({"commit": stem, "workers": workers}), flush=True)


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
