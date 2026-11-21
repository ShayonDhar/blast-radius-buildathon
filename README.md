# Blast Radius selector

`selector.py` picks, per patch, the sqlglot tests it can break, from `maps.json.gz`
(per-line / per-statement-name coverage of the pinned commit, including process-pool
workers, plus data files opened by tests). Rebuild the map with:

    python build_maps.py <sqlglot checkout at eb11aec> <tests.json> maps.json.gz

(run in the labeling environment: `pip install -r requirements.lock coverage`,
`PYTHONPATH` unused; `trace_plugin.py` is the pytest plugin it drives).
