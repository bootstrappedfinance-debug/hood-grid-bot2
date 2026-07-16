# grid-reconciler

Deterministic, stateless grid-trading reconciler (pure Python 3, stdlib only).

- `cloud_reconciler.py` — fixed-geometry reconciler: reads account-state JSON, emits a plan of limit-order cancels/places. No account identifiers, no credentials.
- `grid_engine.py` — full engine (initialization, re-anchoring, persistence).
- `test_*.py` — unit + differential-equivalence tests.

Run tests: `python3 -m unittest discover -v`
