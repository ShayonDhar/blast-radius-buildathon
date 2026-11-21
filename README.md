# Blast Radius selector

`selector.py` picks, per patch, the sqlglot tests it can break, from `maps.json.gz`
(per-line / per-statement-name coverage of the pinned commit, including process-pool
workers, plus data files opened by tests). Rebuild the map with:

    python build_maps.py <sqlglot checkout at eb11aec> <tests.json> maps.json.gz
    python build_maps.py --inst <checkout> maps.json.gz maps.json.gz   # expression classes each test instantiates
    python build_maps.py --imp  <checkout> maps.json.gz maps.json.gz   # code that runs while sqlglot is imported

The two extra passes cover what line coverage cannot see: class attributes consumed when a class is
created (an edit to one selects the tests that instantiate that class), and code run during import
(an edit there, or to a class instantiated during import, selects every test, since a failing
import breaks collection everywhere).

(run in the labeling environment: `pip install -r requirements.lock coverage`,
`PYTHONPATH` unused; `trace_plugin.py` is the pytest plugin it drives).

## Leaderboard variant (branch `claude/leaderboard-attempt`)

This branch adds a runtime gate on top of the static selection. Tests of 3 s or more
(`test_execute_tpcds`, `test_max_nodes`, `test_generator_memory`, the TPC-H tests) are left out
only when all of these hold:

- every edited expression lies inside a function body and has no calls or assignment targets;
- with the patch applied, a few cheap selected tests (150 ms of recorded time, plus up to 150 ms of
  the giant's own test file) pass;
- while they run, each edited expression is evaluated both ways
  (`_br_shadow(i, lambda: old, lambda: new)`, see `shadow_instrument` in `selector.py`), is reached
  at least once, and never gives a different value.

The tests that ran and passed are dropped as well. Any other edit keeps the static answer. The
verifier (`verify.py`) forks a preloaded process per check. It builds `TestExecutor`'s TPC tables and
duckdb views only when a test reads them, which saves ~2 s of CPU per check.

Local results (sample: 30 commits; mutants: 121 with full-suite truth; novel: 20 crafted edits):

| | sample sim | sample misses | mutant misses | novel misses | container CPU (sample) |
|---|---|---|---|---|---|
| static (`a3fe8e3`) | 172.455 | 0 | 0 | 0 | 0.33 s |
| this branch | 130.775 | 0 | 0 | 0 | 4.35 s |

The gate rests on evidence from a sample of tests, not on a proof: it is meant for the practice
board. The final entry stays the static selector.
