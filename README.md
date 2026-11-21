# Blast Radius selector

An entry in the RWX "Blast Radius" buildathon. This README is the briefing for anyone (human or
agent) joining the project: the task, the exact rules, where we stand, how the code works, what has
been tried, and what is still open. Read all of it before changing code.

## Contents

1. [The task](#1-the-task)
2. [Scoring, exactly](#2-scoring-exactly)
3. [Rules and limits](#3-rules-and-limits)
4. [Where we stand](#4-where-we-stand)
5. [How the selector works](#5-how-the-selector-works)
6. [Files](#6-files)
7. [Running and validating](#7-running-and-validating)
8. [What has been tried](#8-what-has-been-tried)
9. [Open problems and ideas](#9-open-problems-and-ideas)
10. [Working rules for this repo](#10-working-rules-for-this-repo)

---

## 1. The task

Every fast CI system decides, for each commit, which tests the change needs. We get a batch of
code changes to **sqlglot** (a Python SQL parser), pinned at commit
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. For each change we say which of its tests to run and how
to split them across up to 8 workers. Most changes break at least one test; some break nothing, and
the right answer for those is to run nothing.

We hand in a public GitHub repository with a `Dockerfile` at its top, plus the commit to score.

**Input** (read-only, inside the container):

- `/data/commits/<id>.patch`: one unified diff per commit, against the pinned sqlglot commit. Each
  patch stands alone; none builds on another.
- `/data/tests.json`: `{"tests": [{"number": 12, "id": "tests/test_parser.py::TestParser::test_union",
  "file": "tests/test_parser.py", "seconds": 0.031}, ...]}`. There are about 1620 tests, numbered from
  0 with no gaps. A `skipped` key names tests that cannot fail.

**Output**: one JSON line per commit on stdout, e.g.
`{"commit": "k7q2", "workers": [[12, 845, 901], [3, 77]]}`. Each inner list holds one worker's test
numbers; `"workers": []` runs nothing. Logs go to stderr. Anything else on stdout, or more than 2 MB,
rejects the run.

**What "fails" means**: pytest reports a failure, an error or a timeout for the test, or its file
cannot be imported. A test with subtests is one test, and one failing subtest fails it. A change
that only adds a comment is not promised to be harmless.

## 2. Scoring, exactly

The full rules are in `SCORING.md` in the sample kit; this is the summary.

- **Per commit**: the busiest worker's recorded seconds, plus **2 s per worker**. An empty selection
  costs 0. Durations are `round(seconds * 1000)` ms; arithmetic is in whole milliseconds.
- **Miss**: if any failing test is left out of a commit, that commit costs **+300 s**.
- **Run total** = sum of commit costs + 300 s × misses + **CPU seconds** used by every process in
  our container (user + system). The build is not charged.
- **Score** = the middle of 3 runs, rounded to 0.1 s. Lower is better. The final reruns the top
  entries 5 times on a quiet machine.
- **Output validity** (any breach rejects the run): standard JSON; exactly the keys `commit` and
  `workers`; a known commit id; no repeated commit; integer test numbers from tests.json; no test
  twice in a line; at most 8 non-empty workers; every commit present.
- **Feedback**: the board reports only each run's simulated seconds, CPU seconds and number of
  missed commits. It never says which commits missed or what we printed.
- **Baseline**: run every test on 3 workers = 1614.1 s.
- **Ties**: an equal score ranks the earlier submission first. In the final, a score at most 1 s
  behind the best counts as equal.

Worked example: tests of 21.5 s, 3 s and 1 s on two workers ([21.5], [3, 1]) cost 21.5 + 2 × 2 = 25.5 s.

## 3. Rules and limits

Quoted from the organisers' sheet, with notes.

**What counts**

- "**Only the final counts. The practice board carries no prize.**"
- "At the cutoff your team's chosen commit (**your best, unless you pick another**) is scored once on
  **commits nobody has seen, including kinds of change the practice set never had**. Then at least the
  5 best entries are scored again on the same commits, 5 runs each instead of 3, one at a time on
  one scoring machine with nothing else running on it."
- Cutoff: **Sunday 11:00 AM ET**. The final has about 1.5× as many commits as practice.
- Consequence: robustness on unseen kinds of change matters more than the practice board. One miss
  costs 300 s, more than all our practice-board waste.

**What is allowed**

- "**No answers worked out ahead of time and looked up by patch: no table from a patch, or from a
  kind of edit, to the tests it breaks.** Maps built from the pinned commit (coverage, imports,
  fixtures) are allowed. **Top entries are read.**" So: no special cases such as "an `is` → `==` edit
  breaks nothing", no tables learned from labelled mutants, no hard-coded answers for sample
  commits, no per-commit answers learned from board probes.
- "Running sqlglot's tests while you decide is allowed. Its CPU time is charged like any other."
- "No sharing code between teams: top entries are compared, and you may be asked to explain yours."
- The build may download only from public package registries (PyPI, npm, Docker Hub), GitHub
  releases at a fixed tag, and sqlglot's own repository at commit eb11aec. Nothing from an address we
  control. The final rebuilds every image after the cutoff.
- "**Keep your repository public, with every commit you submitted still in it, until the results
  are published.**" Never rewrite or delete history on submitted branches.

**Limits**

| Phase | Limits |
|---|---|
| Build | network, 4 cores, 8 GB, 10 minutes; repository may hold 20 MB; no submodules or LFS |
| Run | no network; user `nobody`, `HOME=/tmp`; ≤ 256 processes; 2 cores; 4 GB; read-only disk; `/tmp` 64 MB, empty at start; 3 minutes for the whole batch (4.5 in the final) |

- Submissions: one per hour, made by hand by the team owner.
- A crash, a broken rule, or exceeding the time or memory limit rejects the submission but never
  changes the best score.

## 4. Where we stand

**Final entry: `a3fe8e3b3c3e2148e101b8953e2221c549f42343`** (branch `claude/cool-davinci-4mugkp`,
the static selector). It must be picked explicitly on the site. The default is the best board score,
which is the older `1668ee9`, and that commit missed 4 of the first 15 crafted new-kind edits.

### Board history (practice set)

| Commit | Total | Simulated | CPU | Misses | What it was |
|---|---|---|---|---|---|
| `1668ee9` | **181.4** | 181.138 | 0.23 | 0 | static selector (best board score) |
| `8bf875d` | 192.3 | 179.09 | 13.2 | 0 | + runtime verifier |
| `994c1b8` | 189.6 | 179.09 | 10.55 | 0 | verifier, giant skip off |
| `fa97da8` | ? | ? | ? | ? | diagnostic probe: select nothing (result not recorded) |
| `8fa32bb` | ? | ? | ? | ? | diagnostic probe: drop every test ≥ 2 s (result not recorded) |
| `a3fe8e3` | 182.0 | 181.659 | 0.38 | 0 | **chosen final**: static + robustness fixes |
| `3bc18f7` | 182.1 | 181.659 | 0.39 | 0 | leaderboard branch, strict shadow gate (never fired) |
| `aca4e4d` | pending | | | | leaderboard branch, loose gate |

- The practice-board leader is at **130.8**.
- The board set is *not* the 30 sample commits. Our sample simulated time is 172.455, not
  181.659, and the strict gate fires on two sample commits but on no board commit.

### Local numbers for `a3fe8e3`

| Set | Simulated | Misses | Notes |
|---|---|---|---|
| 30 sample commits | 172.455 s | 0 | floor (exactly the failing tests) is 94.4 s |
| 121 generated mutants with full-suite truth | 1163.5 s | 0 | |
| 24 adversarial patches | — | — | only one widens to all tests |
| 20 crafted new-kind edits | — | 0 | `1668ee9` misses 4 of the first 15 |

- **Complement audit**: for all 142 mutant and crafted commits, every test the selector leaves out
  was run on the patched tree. No test fails.

### Where the sample waste is

Every failing sample commit is already at its floor. All the waste is in commits that break
nothing, but whose selected tests really execute the changed code:

| Commit | Cost | Why |
|---|---|---|
| `2haz`, `66ks` | ~50 s together | `is` → `==` on hot lines; each carries `test_execute_tpcds` (21.5 s) |
| `4d63`, `ktz3` | ~17 s | class-attribute fallbacks |
| `gbav`, `2qn5`, `5y72`, `fryt` | ~2–3 s each | small harmless selections |

Giant tests: `test_max_nodes` 28.8 s, `test_execute_tpcds` 21.5 s, `test_generator_memory` 6.7 s,
`test_execute_tpch` 3.9 s, `test_optimized_tpch` 3.5 s.

## 5. How the selector works

### Build time: the map (`build_maps.py` → `maps.json.gz`)

Built once from the pinned checkout and committed. `trace_plugin.py` runs pytest twice and records:

- per-test line coverage (coverage.py dynamic contexts per test phase), including inside
  ProcessPool workers;
- data files each test opened;
- dialect usage;
- class-attribute reads (via probe descriptors).

Two extra passes cover what line coverage cannot see:

- `--inst` (`inst_plugin.py`): which expression classes each test instantiates, so class-level edits
  (e.g. `arg_types`) select the tests that construct that class.
- `--imp`: code and expression classes executed while importing sqlglot. An edit there selects all
  tests, because a failing import breaks collection everywhere, and `generators/duckdb.py` parses SQL
  templates at import.

Per file the map holds `lines`, `spans`, `all`, `use`, `cuse`, `stmts`, `cstmts`, `dead`, `strs` and
`imp`. Globals: `tests`, `sets` (fixed-width hex bitmasks), `data`, `test_files`, `attrs`, `inst`,
`impcls`.

```
python build_maps.py <sqlglot checkout at eb11aec> <tests.json> maps.json.gz
python build_maps.py --inst <checkout> maps.json.gz maps.json.gz
python build_maps.py --imp  <checkout> maps.json.gz maps.json.gz
```

Run these in the labelling environment (`pip install -r requirements.lock coverage`).

### Run time: `selector.py` (stdlib only)

Each patch is parsed. For each changed Python file, removed lines and insertion points are mapped to
tests through the map.

**Special cases**

| Edit | Selection |
|---|---|
| comment- or blank-only | nothing |
| syntax error, deletion, rename, new import-time code, config file (`pyproject.toml`, `conftest.py`, …) | all tests |
| function-body line no test ever executed (`dead`) | nothing |
| data or fixture file | the tests that opened it |

**Fallbacks**

- An unexecuted line selects the tests that ran its function (`span_mask`).
- A class- or module-level line selects the class span, the users of the statement's names, and the
  importer closure (`cuse`/`use`).
- A dialect-family file selects that dialect's users.

**Robustness rules**

- In test modules, import-time lines (module/class level, def headers with defaults, decorators)
  select every test that touched or imports the file.
- An `imp` line selects all tests.
- A class-level line in an expression class selects its instantiators (`inst`); if that class is
  instantiated at import (`impcls`), all tests.

**Giants**: `test_max_nodes` and `test_generator_memory` are pruned when only peripheral files changed
and the edited lines don't directly reach them (`prune_giants`).

**Workers**: an LPT split, choosing k = 1..8 to minimise max load + 2 s × k (optimal on the sample).
Cheap tests from the edited file's coverage fill idle capacity for free.

**Never crash**: any exception, or an unreadable map, falls back to all tests.

### Optional runtime verifier (`verify.py`; off in the final)

A zygote process preloads pytest, duckdb and pandas, then forks one child per check. The child sees an
overlay: a symlink farm of the pristine tree with the patched files written as real files. It runs
pytest in-process, with a synchronous ProcessPool stand-in that keeps the pickle round trip. Switches
(environment variables):

- `BR_VERIFY`: on/off.
- `BR_SMALL_MS`: verify whole small selections and keep only the failing tests (exact).
- `BR_GATE`, `BR_GATE_MS`, `BR_SENT_MS`, `BR_SAME_MS`, `BR_GIANT_MS`: the giant gate.
- `BR_SHADOW`, `BR_SHADOW_STRICT`: shadow evaluation (leaderboard branch only).

### Leaderboard variant (branch `claude/leaderboard-attempt`)

A runtime gate leaves out the giants when a few cheap selected tests pass on the patched code.

- **Shadow evaluation**: `shadow_instrument` rewrites each edited expression (inside a function body,
  no calls, no assignment targets) to `_br_shadow(i, lambda: old, lambda: new)`. This returns the new
  value and records whether the old one would have differed. A divergence keeps the giants. This
  catches mutant `t6x013` (`not x` → `not not x`, which fails only `test_execute_tpcds`).
- **Lazy TPC setup**: in the verifier child, `TestExecutor`'s TPC tables and duckdb views are built
  only when a test reads them, saving ~2 s of CPU per check.
- `3bc18f7` was **strict** (giants dropped only with shadow evidence). It never fired on the board.
- `aca4e4d` is **loose**: non-instrumentable edits are gated on 800 ms of cheap tests passing, and only
  tests ≥ 7 s are gated. Locally: sample 130.5 s, mutants 1010.1 s, 0 misses on sample, mutants and
  crafted edits.
- This is evidence from a sample of tests, not a proof. It is meant for the practice board only.

## 6. Files

| File | Purpose |
|---|---|
| `selector.py` | the program the image runs |
| `maps.json.gz` | the coverage map (committed; built by `build_maps.py`) |
| `build_maps.py` | builds the map from a pinned checkout (`--inst`, `--imp` passes) |
| `trace_plugin.py` | build-time pytest plugin: coverage contexts, data files, dialects, attribute reads |
| `inst_plugin.py` | build-time pytest plugin: expression classes instantiated per test |
| `verify.py` | optional runtime verifier (zygote, fork, overlay) |
| `requirements.lock` | the labelling environment's packages (used by the verifier image) |
| `Dockerfile` | `cool-davinci` branch: slim image (sqlglot `.py` sources only, from codeload at eb11aec); leaderboard branch: full image with pytest, duckdb, pandas, test fixtures |

## 7. Running and validating

You need the sample kit (`blast-radius-sample.tar.gz`: `sample/commits`, `sample/tests.json`,
`sample/answers.json`, `score.py`, `Makefile`, `SCORING.md`) and a sqlglot checkout at eb11aec.

```
BR_DATA=<kit>/sample BR_SRC=<sqlglot checkout> BR_MAP=maps.json.gz python selector.py > out.jsonl
python <kit>/score.py <kit>/sample <kit>/sample/answers.json out.jsonl
make sample   # in the kit, with our Dockerfile: the real image under the scorer's run flags
```

- `BR_WALL` caps verifier wall time (default 90 s).
- `BR_DEBUG=1` logs verifier decisions to stderr.

**Ground truth for a new patch**

1. Apply it to a copy of the checkout.
2. Run `SKIP_INTEGRATION=0 pytest -n 4 --timeout=120 -rfE tests`. This takes 5–10 minutes.
3. Collect the FAILED, ERROR and SUBFAILED node ids from the short test summary.

**Careful**: subtest names can contain multi-line SQL, which puts the test id on a later line. Join
continuation lines before extracting ids, or you will undercount failures.

**Before shipping any change**

1. 0 misses on the sample.
2. 0 misses on the mutant set with full-suite truth.
3. 0 misses on the crafted new-kind edits.
4. The image runs under the Makefile's run flags, well inside 180 s.
5. CPU measured.

Never ship anything with a miss on known truth.

## 8. What has been tried

Do not re-propose these without new evidence.

| Idea | Result |
|---|---|
| Edit-kind equivalence rules (e.g. `is` ↔ `==` with a None/Enum operand) | rejected: a table from a kind of edit (rule above), and fitted to two sample commits |
| Replaying functions on a capped sample of recorded inputs | unsound; one 300 s miss outweighs the gains |
| Dropping giants when the other selected tests pass | unsafe without shadow evidence: `t6x013` fails only in `test_execute_tpcds`; a sentinel subset missed 3 of ~17 giant-failing mutants; `ns6` (enum value change) fails only in `test_generator_memory` |
| Runtime verification of small selections on the board | saved ~2 s simulated for 10–13 s CPU (`8bf875d`, `994c1b8`); per-run overhead is ~2.5× higher on the board than locally |
| Per-query TPC-DS selection | most edits touch 57 of 99 queries; even with stored duckdb results this costs ~16 s CPU against 21.5 s saved |
| `attr_mask` falling back to `dialect_users` | wider: sample 172.5 → 172.9, mutants 1161.5 → 1195.0; reverted |
| Extended giant pruning by file | looked safe on stored truth, but that truth undercounted multi-line subtest failures; reverted |
| Strict shadow gate on the board | identical to static: no board commit qualified |

## 9. Open problems and ideas

- **Holes on unseen kinds of change** (highest value for the final). Candidates not yet probed:
  - multi-file edits
  - moving or renaming functions within a file
  - helpers used through `functools.partial` or dispatch dicts (`TRANSFORMS`, `FUNCTIONS`, `PARSERS`)
  - lazy imports (`sqlglot.dialects.__getattr__`)
  - `functools.lru_cache`
  - pickling and serde
  - tests that run sqlglot in a subprocess (`test_lazy_load`)
  - fixture-format changes
  - order-dependent tests

  For each: write a patch, get full-suite truth, and compare it with the selection.
- **Board waste is unknown.** The `8fa32bb` probe (drop every test ≥ 2 s) gives about the floor any
  giant-skipping approach can reach on the board. If its simulated time is above ~131, no gate tuning
  beats the leader.
- **Sound precision gains** on class-attribute fallbacks (`4d63`, `ktz3`): record which dict keys and
  attributes each test reads, then select only the readers of the changed key.
- **Re-measure exact small-selection verification** (`BR_SMALL_MS=1500`) with the cheaper verifier.
  Locally it saved 8.3 s of simulated time for 4.45 s of CPU.

## 10. Working rules for this repo

- Branches:
  - `claude/cool-davinci-4mugkp`: the final entry (static).
  - `claude/leaderboard-attempt`: practice-board experiments.
- Do not force-push or delete submitted commits; the repository must stay public with them.
- Submissions are made by hand by the team owner, one per hour. After a leaderboard submission,
  re-pick `a3fe8e3` as the final entry, so a riskier commit doesn't become the default.
- Report a claim with evidence: the command run, its output, and miss counts on the validation sets.
