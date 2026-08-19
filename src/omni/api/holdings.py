"""The personal position tracker: manual holdings, read and written.

The write path for `manual_holding`, plus valuation from the claim store.
It lives beside `api/portfolio.py` for the same reason that module left
`trading.py`: these are writes, and they stay out of the read modules.

Three rules carried from the rest of the system:

**The owner is the authenticated principal, never the body.** Holdings are
user-scoped like every audience-bearing read; a body-supplied owner is the
front-door leak the auth boundary exists to close.

**Numbers arrive as strings and parse as Decimal.** A JSON number is a
float by the time it arrives, and 0.1 as a float is not 0.1; that error
would live in `quantity` and `cost_basis` forever.

**The price is never taken from the caller.** Each holding is valued from
the latest `price_snapshot` the caller's audience may see -- the same
`visible_claims_cte` Discover reads -- and a holding the store cannot price
is returned with `null` value and an explicit `unpriced` reason, not with a
zero. A symbol with no entity in the store is refused on write: tracking a
name the system has no data for would fabricate coverage. Quantity edits
and deletes are the user correcting their own notebook, which is why this
table is not append-only: it is not evidence.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from uuid import UUID

from neutron import App, Router
from neutron.error import bad_request, not_found, unauthorized
from pydantic import BaseModel
from starlette.requests import Request

from omni.auth import resolve_audience_from_request
from omni.coverage.visibility import visible_claims_cte

# The latest price a holding's audience may see for its symbol. knowledge_date
# DESC breaks the tie between two prints of the same session, so a corrected
# late print wins over the first one. $2 is an optional holding id; callers
# pass NULL to list every holding for the user.
_PRICED_HOLDINGS = f"""
WITH visible AS ({visible_claims_cte("$1")}),
priced AS (
    SELECT DISTINCT ON (h.id)
           h.id, (c.value ->> 'close')::numeric AS price,
           c.event_date AS price_date
    FROM manual_holding h
    JOIN entity e ON e.symbol = h.symbol
    JOIN visible c ON c.entity_id = e.id AND c.claim_type = 'price_snapshot'
    WHERE h.user_id = $1 AND ($2::uuid IS NULL OR h.id = $2)
    ORDER BY h.id, c.event_date DESC, c.knowledge_date DESC
)
SELECT h.id, h.symbol, h.quantity, h.cost_basis, h.currency, h.note,
       h.created_at, h.updated_at, p.price, p.price_date
FROM manual_holding h
LEFT JOIN priced p ON p.id = h.id
WHERE h.user_id = $1 AND ($2::uuid IS NULL OR h.id = $2)
ORDER BY h.symbol
"""

_ENTITY_FOR_SYMBOL = "SELECT id FROM entity WHERE symbol = $1 LIMIT 1"


def _decimal_field(raw: str | None, name: str, *, zero_ok: bool) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise bad_request(f"{name} is not a number: {raw!r}") from exc
    if not value.is_finite():
        raise bad_request(f"{name} must be finite, got {raw!r}")
    if value <= 0 and not (zero_ok and value == 0):
        raise bad_request(f"{name} must be positive, got {raw!r}")
    return value


def _require_user(request: Request) -> UUID:
    user = resolve_audience_from_request(request)
    if user is None:
        raise unauthorized("Authentication required")
    return user


class HoldingIn(BaseModel):
    symbol: str
    quantity: str
    cost_basis: str | None = None
    note: str | None = None


class HoldingPatch(BaseModel):
    quantity: str | None = None
    cost_basis: str | None = None
    note: str | None = None


def _holding_payload(row) -> dict:
    price = row["price"]
    quantity = row["quantity"]
    basis = row["cost_basis"]
    priced = price is not None
    payload = {
        "id": str(row["id"]),
        "symbol": row["symbol"],
        "quantity": float(quantity),
        "cost_basis": float(basis) if basis is not None else None,
        "currency": row["currency"],
        "note": row["note"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "last_price": float(price) if priced else None,
        "price_as_of": row["price_date"].isoformat() if priced else None,
        "value": float(price * quantity) if priced else None,
        "unrealized_pnl": (
            float(price * quantity - basis)
            if priced and basis is not None
            else None
        ),
        "valuation": "priced" if priced else "unpriced",
    }
    return payload


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/holdings")
    async def list_holdings(request: Request) -> dict:
        user = _require_user(request)
        rows = await app.db.pool.fetch(_PRICED_HOLDINGS, user, None)
        holdings = [_holding_payload(row) for row in rows]
        priced = [h for h in holdings if h["valuation"] == "priced"]
        # The total is stated only when every holding priced: summing the
        # priced subset would present a portfolio smaller than the one held,
        # and summing missing prices as zero would invent wealth.
        complete = len(priced) == len(holdings) and holdings != []
        return {
            "holdings": holdings,
            "summary": {
                "positions": len(holdings),
                "priced": len(priced),
                "total_value": (
                    sum(h["value"] for h in priced) if complete else None
                ),
                "total_pnl": (
                    sum(h["unrealized_pnl"] for h in priced if h["unrealized_pnl"] is not None)
                    if complete and all(h["cost_basis"] is not None for h in priced)
                    else None
                ),
            },
        }

    @router.post("/holdings")
    async def add_holding(body: HoldingIn, request: Request) -> dict:
        user = _require_user(request)
        symbol = body.symbol.strip().upper()
        if not symbol:
            raise bad_request("symbol is required")
        quantity = _decimal_field(body.quantity, "quantity", zero_ok=False)
        assert quantity is not None
        basis = _decimal_field(body.cost_basis, "cost_basis", zero_ok=True)
        entity = await app.db.pool.fetchval(_ENTITY_FOR_SYMBOL, symbol)
        if entity is None:
            raise bad_request(
                f"nothing in the store tracks {symbol!r}; a holding the system "
                f"cannot price would show a fabricated value"
            )
        try:
            row = await app.db.pool.fetchrow(
                """
                INSERT INTO manual_holding (user_id, symbol, quantity, cost_basis, note)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id, symbol, currency) DO UPDATE SET
                    quantity = EXCLUDED.quantity,
                    cost_basis = EXCLUDED.cost_basis,
                    note = EXCLUDED.note,
                    updated_at = now()
                RETURNING id
                """,
                user, symbol, quantity, basis, body.note,
            )
        except Exception as exc:
            raise bad_request(f"could not save the holding: {exc}") from exc
        assert row is not None
        priced = await app.db.pool.fetchrow(_PRICED_HOLDINGS, user, row["id"])
        return _holding_payload(priced)

    @router.patch("/holdings/{holding_id}")
    async def edit_holding(holding_id: UUID, body: HoldingPatch, request: Request) -> dict:
        user = _require_user(request)
        quantity = _decimal_field(body.quantity, "quantity", zero_ok=False)
        basis = _decimal_field(body.cost_basis, "cost_basis", zero_ok=True)
        row = await app.db.pool.fetchrow(
            """
            UPDATE manual_holding
            SET quantity = COALESCE($3, quantity),
                cost_basis = COALESCE($4, cost_basis),
                note = COALESCE($5, note),
                updated_at = now()
            WHERE id = $1 AND user_id = $2
            RETURNING id
            """,
            holding_id, user, quantity, basis, body.note,
        )
        if row is None:
            raise not_found(f"no holding {holding_id} for this account")
        priced = await app.db.pool.fetchrow(_PRICED_HOLDINGS, user, holding_id)
        return _holding_payload(priced)

    @router.delete("/holdings/{holding_id}")
    async def remove_holding(holding_id: UUID, request: Request) -> dict:
        user = _require_user(request)
        removed = await app.db.pool.fetchval(
            "DELETE FROM manual_holding WHERE id = $1 AND user_id = $2 RETURNING id",
            holding_id, user,
        )
        if removed is None:
            raise not_found(f"no holding {holding_id} for this account")
        return {"removed": str(removed)}

    return router
