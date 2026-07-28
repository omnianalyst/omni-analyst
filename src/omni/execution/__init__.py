"""Execution tier: the layer that acts on the world.

A broker whose SDK is absent raises `ImportError`; it never returns a
success-shaped result. Execution writes no claim and feeds no calibration.
See `broker.py` for the facade and the order value object, `alpaca.py` and
`ibkr.py` for the two client implementations.
"""

from omni.execution.broker import (
    Broker,
    BrokerType,
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
)

__all__ = [
    "Broker",
    "BrokerType",
    "OrderRequest",
    "OrderSide",
    "OrderType",
    "TimeInForce",
]
