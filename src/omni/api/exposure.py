"""POST /exposure/overlap -- what is this portfolio actually exposed to.

The composition (which ETFs, at what allocations) arrives in the body because
it is a statement about a portfolio the caller is asking about, not a fact the
coverage layer owns. The response is the overlap/concentration/bucket analysis
that ``overlap.analyze`` produces, rendered against holdings read from the
store through the same visibility filter every claim read goes through.

This is a **read endpoint**. It queries the claim table and computes; it writes
nothing. Deliberately in its own module rather than extended onto
``api/trading.py``, whose own header says "read-only with no exceptions" -- a
POST among those GETs is one copied decorator away from a side effect, and the
exposure view is refreshed on demand by an operator deciding allocation.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from uuid import UUID

from neutron import App, Router
from neutron.error import bad_request
from pydantic import BaseModel
from starlette.requests import Request

from omni.auth import resolve_audience_from_request
from omni.exposure.overlap import analyze
from omni.exposure.query import CompositionEntry, CompositionError, load_positions


class PositionIn(BaseModel):
    symbol: str
    allocation: str
    bucket: str | None = None


class OverlapIn(BaseModel):
    positions: list[PositionIn]
    concentration_threshold: str | None = None
    overlap_threshold: str | None = None


def _audience(request: Request) -> UUID | None:
    return resolve_audience_from_request(request)


def _decimal_allocation(raw: str, symbol: str) -> Decimal:
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise bad_request(
            f"allocation for {symbol} is not a number: {raw!r}"
        ) from exc
    if not value.is_finite():
        raise bad_request(f"allocation for {symbol} must be finite, got {raw!r}")
    if value < 0:
        raise bad_request(
            f"allocation for {symbol} must not be negative, got {raw!r}"
        )
    return value


def _payload(result) -> dict:
    return {
        "concentration": [
            {
                "ticker": c.ticker,
                "total_weight": str(c.total_weight),
                "source_etfs": list(c.source_etfs),
            }
            for c in result.concentration
        ],
        "overlaps": [
            {
                "etf_a": o.etf_a,
                "etf_b": o.etf_b,
                "shared_weight": str(o.shared_weight),
            }
            for o in result.overlaps
        ],
        "bucket_exposure": [
            {"bucket": b, "allocation": str(w)}
            for b, w in result.bucket_exposure
        ],
        "top_holdings": [
            {"ticker": t, "weight": str(w)}
            for t, w in result.top_holdings
        ],
    }


def build_router(app: App) -> Router:
    router = Router()

    @router.post("/exposure/overlap", status_code=200)
    async def overlap(body: OverlapIn, request: Request) -> dict:
        if not body.positions:
            raise bad_request("positions must not be empty")
        composition = [
            CompositionEntry(
                symbol=p.symbol,
                allocation=_decimal_allocation(p.allocation, p.symbol),
                bucket=p.bucket,
            )
            for p in body.positions
        ]

        audience = _audience(request)
        try:
            positions = await load_positions(
                app.db.pool,
                composition=composition,
                audience=audience,
            )
        except CompositionError as exc:
            raise bad_request(str(exc)) from exc

        kwargs: dict[str, Decimal] = {}
        if body.concentration_threshold is not None:
            kwargs["concentration_threshold"] = _decimal_allocation(
                body.concentration_threshold, "concentration_threshold"
            )
        if body.overlap_threshold is not None:
            kwargs["overlap_threshold"] = _decimal_allocation(
                body.overlap_threshold, "overlap_threshold"
            )

        result = analyze(positions, **kwargs)
        return _payload(result)

    return router


__all__ = ["build_router"]
