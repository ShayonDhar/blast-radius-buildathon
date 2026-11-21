"""Build the coverage / fixture map from a pinned sqlglot checkout.

python build_maps.py <checkout> <tests.json> <out.json.gz> [--jobs N] [--shuffle SEED]

Runs the whole test suite in shards under line coverage with one context per
test phase (see trace_plugin.py), then folds everything into:
  tests:  [test id, ...]
  sets:   [hex bitmask over tests, ...]   (interned)
  files:  {relpath: {"lines": [[start, end, set], ...],
                     "spans": [[start, end, kind, set], ...],   # def/class spans
                     "all": set}}
  data:   {relpath of opened data file: set}
"""

import ast
import concurrent.futures as cf
import gzip
import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

import coverage

HERE = Path(__file__).resolve().parent


def shard_files(tests, jobs):
    secs = {}
    for t in tests:
        secs[t["file"]] = secs.get(t["file"], 0) + t["seconds"]
    loads = [0.0] * jobs
    shards = [[] for _ in range(jobs)]
    for f, s in sorted(secs.items(), key=lambda kv: -kv[1]):
        i = loads.index(min(loads))
        shards[i].append(f)
        loads[i] += s
    return [s for s in shards if s]


def run_shard(checkout, files, prefix, seed):
    env = dict(os.environ)
    env.update(
        BR_OUT=prefix,
        SKIP_INTEGRATION="0",
        PYTHONPATH=f"{HERE}:{checkout}",
        PYTHONHASHSEED="0",
    )
    cmd = [sys.executable, "-m", "pytest", "-p", "trace_plugin", "-p", "no:cacheprovider",
           "-q", "-x" if False else "-q", "--timeout=600", *files]
    if seed is not None:
        random.Random(seed).shuffle(files)
        cmd = cmd[:-len(files)] + files
    r = subprocess.run(cmd, cwd=checkout, env=env, capture_output=True, text=True)
    sys.stderr.write(r.stdout[-1500:] + r.stderr[-500:])
    return r.returncode


def spans_of(path):
    try:
        tree = ast.parse(Path(path).read_text())
    except Exception:
        return []
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = min([n.lineno] + [d.lineno for d in n.decorator_list])
            kind = "c" if isinstance(n, ast.ClassDef) else "f"
            out.append((start, n.end_lineno, kind))
    return out


def stmt_ranges(path):
    """(first, last) line of every statement header (compound bodies excluded)."""
    try:
        tree = ast.parse(Path(path).read_text())
    except Exception:
        return []
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.stmt):
            body = getattr(n, "body", None)
            if isinstance(body, list) and body:
                last = body[0].lineno - 1
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    last = max(last, n.lineno)
            else:
                last = n.end_lineno
            if last > n.lineno:
                out.append((n.lineno, last))
    return out


def top_stmts(path, subs=None):
    """Statements outside function bodies: [(start, end, names)].
    names = identifiers whose users may be affected (defined names + outermost class)."""
    try:
        tree = ast.parse(Path(path).read_text())
    except Exception:
        return []
    out = []

    def targets(t):
        if isinstance(t, ast.Name):
            return {t.id}
        if isinstance(t, (ast.Tuple, ast.List)):
            r = set()
            for e in t.elts:
                r |= targets(e)
            return r
        return set()

    def walk(body, outer):
        for st in body:
            start = min([st.lineno] + [d.lineno for d in getattr(st, "decorator_list", [])])
            names = set()
            if outer:
                names.add(outer)
                names |= (subs or {}).get(outer, set())
            # Inside a class only the outermost class name counts: its attributes
            # (arg_types, TRANSFORMS, ...) are reached through its instances, and
            # generic attribute names would match half the code base.
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not outer:
                    names.add(st.name)
                out.append((start, st.end_lineno, names))
            elif isinstance(st, ast.ClassDef):
                if not outer:
                    names.add(st.name)
                out.append((start, st.end_lineno, names))
                walk(st.body, outer or st.name)
            else:
                if not outer:
                    if isinstance(st, ast.Assign):
                        for t in st.targets:
                            names |= targets(t)
                    elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
                        names |= targets(st.target)
                out.append((st.lineno, st.end_lineno, names))
                for field in ("body", "orelse", "finalbody"):
                    sub = getattr(st, field, None)
                    if isinstance(sub, list):
                        walk(sub, outer)
                for h in getattr(st, "handlers", []):
                    walk(h.body, outer)

    walk(tree.body, None)
    return out


def subclass_closure(checkout, rels):
    """class name -> names of all (transitive) subclasses defined in the checkout."""
    direct = {}
    for rel in rels:
        try:
            tree = ast.parse(Path(checkout, rel).read_text())
        except Exception:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef):
                for b in n.bases:
                    bn = b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else None
                    if bn:
                        direct.setdefault(bn, set()).add(n.name)
    out = {}
    for c in direct:
        seen, todo = set(), [c]
        while todo:
            for d in direct.get(todo.pop(), ()):
                if d not in seen:
                    seen.add(d)
                    todo.append(d)
        out[c] = seen
    return out


def module_of(rel):
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def import_graph(checkout, rels):
    """rel -> set of rels it imports (sqlglot modules only)."""
    mod_to_rel = {module_of(r): r for r in rels if r.startswith("sqlglot/")}
    graph = {}
    for rel in rels:
        if not rel.startswith("sqlglot/"):
            continue
        try:
            tree = ast.parse(Path(checkout, rel).read_text())
        except Exception:
            continue
        pkg = module_of(rel).split(".")
        if not rel.endswith("__init__.py"):
            pkg = pkg[:-1]
        deps = set()
        for n in ast.walk(tree):
            names = []
            if isinstance(n, ast.Import):
                names = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                base = n.module or ""
                if n.level:
                    base = ".".join(pkg[: len(pkg) - n.level + 1] + ([base] if base else []))
                names = [base] + [base + "." + a.name for a in n.names]
            for nm in names:
                if nm in mod_to_rel:
                    deps.add(mod_to_rel[nm])
        graph[rel] = deps
    return graph


def string_ranges(path):
    """Line ranges (first, last) of multi-line string tokens, f-strings included:
    a '#' line inside one is text, not a comment."""
    import tokenize

    out, stack = [], []
    fs = getattr(tokenize, "FSTRING_START", -1)
    fe = getattr(tokenize, "FSTRING_END", -2)
    try:
        with open(path, "rb") as fh:
            for t in tokenize.tokenize(fh.readline):
                if t.type == tokenize.STRING and t.end[0] > t.start[0]:
                    out.append([t.start[0], t.end[0]])
                elif t.type == fs:
                    stack.append(t.start[0])
                elif t.type == fe and stack:
                    first = stack.pop()
                    if t.end[0] > first:
                        out.append([first, t.end[0]])
    except Exception:
        return [[1, 10**9]]  # unknown: treat the whole file as string
    return out


def pack(result, checkout, out):
    """Runtime layout: per-file entries are JSON strings parsed on demand and all
    bitsets live in one fixed-width hex string, so the selector pays only for what it touches."""
    width = max(1, (len(result["tests"]) + 3) // 4)
    hexes = [h.rjust(width, "0") for h in result["sets"]]
    files = {}
    for rel, e in result["files"].items():
        e = dict(e)
        e["strs"] = string_ranges(os.path.join(checkout, rel))
        files[rel] = json.dumps(e, separators=(",", ":"))
    packed = dict(result, sets="".join(hexes), width=width, files=files)
    with gzip.open(out, "wt", compresslevel=9) as f:
        json.dump(packed, f, separators=(",", ":"))


def reformat(old, new, checkout):
    """Convert an old-layout map (no strs, list of sets) without re-running tests."""
    with gzip.open(old, "rt") as fh:
        d = json.load(fh)
    pack(d, checkout, new)


def main():
    args = sys.argv[1:]
    if args and args[0] == "--reformat":
        reformat(args[1], args[2], args[3])
        return
    checkout, tests_json, out = args[0], args[1], args[2]
    jobs = int(args[args.index("--jobs") + 1]) if "--jobs" in args else 4
    seed = int(args[args.index("--shuffle") + 1]) if "--shuffle" in args else None
    checkout = str(Path(checkout).resolve())
    tests = json.loads(Path(tests_json).read_text())["tests"]
    ids = [t["id"] for t in tests]
    idx = {t: i for i, t in enumerate(ids)}
    by_file = {}
    for i, t in enumerate(ids):
        by_file.setdefault(t.split("::")[0], []).append(i)
    by_class = {}
    for i, t in enumerate(ids):
        by_class.setdefault(t.rsplit("::", 1)[0], []).append(i)

    def mask(indices):
        m = 0
        for i in indices:
            m |= 1 << i
        return m

    file_mask = {f: mask(v) for f, v in by_file.items()}
    class_mask = {c: mask(v) for c, v in by_class.items()}

    def ctx_mask(ctx):
        """Bitmask of tests a coverage/open context is charged to."""
        if ctx.startswith("C|"):
            nid = ctx[2:]
            f = nid.split("::")[0]
            if nid == f:
                if f in file_mask:
                    return file_mask[f]
                m = 0  # a package / directory collector
                for k, v in file_mask.items():
                    if k.startswith(f.rstrip("/") + "/"):
                        m |= v
                return m
            m = 0
            for c, cm in class_mask.items():
                if c == nid or c.startswith(nid + "::"):
                    m |= cm
            return m
        if "|" not in ctx:
            return 0
        nid, phase = ctx.rsplit("|", 1)
        if phase in ("setup", "teardown"):
            return class_mask.get(nid.rsplit("::", 1)[0], 0) | (1 << idx[nid] if nid in idx else 0)
        return 1 << idx[nid] if nid in idx else 0

    cache = {}

    def cm(ctx):
        if ctx not in cache:
            cache[ctx] = ctx_mask(ctx)
        return cache[ctx]

    cache_path = os.environ.get("BR_CACHE")
    if cache_path and os.path.exists(cache_path):
        import pickle

        line_mask, data_mask, use_mask = pickle.load(open(cache_path, "rb"))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            # Two passes with different sharding / file order: lines that only get credited
            # to whichever test warms a cache first end up credited to both orders.
            prefixes, jobs_list = [], []
            for pi, (pj, pseed) in enumerate([(jobs, seed), (max(jobs - 1, 1), 7)]):
                for si, sh in enumerate(shard_files(tests, pj)):
                    prefixes.append(os.path.join(tmp, f"p{pi}s{si}"))
                    jobs_list.append((sh, pseed))
            with cf.ThreadPoolExecutor(4) as ex:
                rcs = list(ex.map(lambda a: run_shard(checkout, a[0][0], a[1], a[0][1]), zip(jobs_list, prefixes)))
            print("shard return codes", rcs, file=sys.stderr)

            line_mask = {}  # file -> {line: mask}
            data_mask = {}
            use_mask = {}
            import glob

            covs = []
            for p in prefixes:
                covs.append(p + ".cov")
                covs += sorted(glob.glob(p + ".w*.cov"))
            useds = []
            for p in prefixes:
                useds += sorted(glob.glob(p + ".w*.used.json"))
            for c in covs:
                cd = coverage.CoverageData(basename=c)
                cd.read()
                for fn in cd.measured_files():
                    rel = os.path.relpath(fn, checkout)
                    lm = line_mask.setdefault(rel, {})
                    for line, ctxs in cd.contexts_by_lineno(fn).items():
                        m = 0
                        for c in ctxs:
                            m |= cm(c)
                        if m:
                            lm[line] = lm.get(line, 0) | m
            for p in prefixes:
                oj = json.load(open(p + ".opens.json"))
                for ctx, files in oj["opens"].items():
                    m = cm(ctx)
                    for f in files:
                        if not f.endswith((".py", ".pyc")):
                            data_mask[f] = data_mask.get(f, 0) | m
                for ctx, files in oj["used"].items():
                    m = cm(ctx)
                    for f in files:
                        use_mask[f] = use_mask.get(f, 0) | m
            for u in wopens:
                for ctx, files in json.load(open(u)).items():
                    m = cm(ctx)
                    for f in files:
                        if not f.endswith((".py", ".pyc")):
                            data_mask[f] = data_mask.get(f, 0) | m
            for u in useds:
                for ctx, files in json.load(open(u)).items():
                    m = cm(ctx)
                    for f in files:
                        use_mask[f] = use_mask.get(f, 0) | m

        if cache_path:
            import pickle

            pickle.dump((line_mask, data_mask, use_mask), open(cache_path, "wb"))

    sets, set_id = [], {}

    def sid(m):
        if m not in set_id:
            set_id[m] = len(sets)
            sets.append(m)
        return set_id[m]

    for rel, lm in line_mask.items():
        for a, b in stmt_ranges(os.path.join(checkout, rel)):
            m = 0
            for line in range(a, b + 1):
                m |= lm.get(line, 0)
            if m:
                for line in range(a, b + 1):
                    lm[line] = lm.get(line, 0) | m
    # every source file gets an entry, even if no test executed a line in it
    for sub in ("sqlglot", "tests"):
        for f in Path(checkout, sub).rglob("*.py"):
            line_mask.setdefault(os.path.relpath(f, checkout), {})
    graph = import_graph(checkout, list(line_mask))
    importers = {}
    for r, deps in graph.items():
        for d in deps:
            importers.setdefault(d, set()).add(r)

    def closure(rel):
        seen, todo = {rel}, [rel]
        while todo:
            for r in importers.get(todo.pop(), ()):
                if r not in seen:
                    seen.add(r)
                    todo.append(r)
        return seen

    subs = subclass_closure(checkout, list(line_mask))
    stmts = {rel: top_stmts(os.path.join(checkout, rel), subs) for rel in line_mask}
    needed = set()
    for lst in stmts.values():
        for _, _, names in lst:
            needed |= names
    refs = {}
    for rel, lm in line_mask.items():
        if not lm:
            continue
        try:
            tree = ast.parse(Path(checkout, rel).read_text())
        except Exception:
            continue
        for n in ast.walk(tree):
            nm = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else None
            if nm in needed:
                m = lm.get(n.lineno, 0)
                if m:
                    refs[nm] = refs.get(nm, 0) | m

    files_out = {}
    for rel, lm in sorted(line_mask.items()):
        runs = []
        for line in sorted(lm):
            s = sid(lm[line])
            if runs and runs[-1][1] == line - 1 and runs[-1][2] == s:
                runs[-1][1] = line
            else:
                runs.append([line, line, s])
        allm = 0
        for m in lm.values():
            allm |= m
        spans = []
        for (a, b, kind) in spans_of(os.path.join(checkout, rel)):
            m = 0
            for line, lmk in lm.items():
                if a <= line <= b:
                    m |= lmk
            spans.append([a, b, kind, sid(m)])
        um = 0
        for r in closure(rel):
            um |= use_mask.get(r, 0)
        direct = use_mask.get(rel, 0)
        st_out = []
        for a, b, names in stmts[rel]:
            m = 0
            for nm in names:
                m |= refs.get(nm, 0)
            st_out.append([a, b, sid(m)])
        files_out[rel] = {"lines": runs, "spans": spans, "all": sid(allm), "use": sid(direct | allm),
                          "cuse": sid(um | allm), "stmts": st_out}
    data_out = {f: sid(m) for f, m in sorted(data_mask.items())}
    result = {
        "tests": ids,
        "sets": [format(m, "x") for m in sets],
        "files": files_out,
        "data": data_out,
        "test_files": {f: sid(m) for f, m in file_mask.items()},
    }
    pack(result, checkout, out)
    print(f"wrote {out}: {len(sets)} sets, {len(files_out)} files, {len(data_out)} data files",
          file=sys.stderr)


if __name__ == "__main__":
    main()
