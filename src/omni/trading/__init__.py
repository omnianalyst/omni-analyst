"""The trading tier: the one-way bridge from conviction to capital.

`policy.py` decides which prediction methods may hold capital at all, reading
the same `calibration_bucket` the conviction gate reads. `bridge.py` turns a
prediction into a sized, bounded `TradeIntent`. `router.py` picks the venue
where the edge survives its own cost model. `loop.py` runs the path.

**The one-way rule.** This package may import `omni.conviction`,
`omni.portfolio` and `omni.venue`. Nothing in `omni.conviction` or
`omni.capabilities` may ever import this package, `omni.portfolio` or
`omni.venue`. A fill must not influence a prediction, a calibration bucket or a
gate threshold -- that is how a system starts grading its own homework and the
hit rate quietly stops describing anything. Enforced by an AST scan in
`tests/test_trading_isolation.py`, in the style of the existing
`test_execution.py::test_*_imports_nothing_from_*`.

Exports are wired deliberately after the modules land; import from the modules
directly rather than adding names here.
"""
