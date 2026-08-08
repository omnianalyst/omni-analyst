"""The portfolio tier: what is held, what may be risked, and how much.

`state.py` holds positions, cash and NAV, materialised from the order ledger
rather than kept as an independent source of truth. `orders.py` is that ledger.
`risk.py` refuses intents that violate a limit, and refuses when it cannot tell.
`sizing.py` turns a calibrated hit rate and a barrier pair into a quantity.
`reconcile.py` compares local state to venue truth and halts on divergence.

This package describes and constrains. It never calls a venue: that is
`trading/`, which may import this package, while nothing here may import
`trading/` or `venue/` implementations. `tests/test_trading_isolation.py`
enforces the direction mechanically.

Exports are wired deliberately after the modules land; import from the modules
directly rather than adding names here.
"""
