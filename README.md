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
