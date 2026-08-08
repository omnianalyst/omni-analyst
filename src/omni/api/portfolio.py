"""Opening a book -- the write path over portfolio state.

Deliberately not in `api/trading.py`. That module states its own rule: it is
read-only with no exceptions, because an operator refreshes it while deciding
whether to commit capital and a report with a side effect fires exactly when
nobody is watching for one. A POST sitting among those GETs is one copied
decorator away from being reachable by a refresh, so the write path lives in its
own module and the read module stays a module that cannot write.

Two things decide whether this endpoint is safe, and both are about where a
value comes from rather than what it is.

**The owner is the authenticated principal and is never read from the body.**
`portfolio.user_id` is the key every audience-scoped read path resolves on, so a
body-supplied owner would let one account open a book owned by another -- the
same leak the audience scoping exists to prevent, arriving through the front
door. The body model has no owner field and the handler never looks for one.

**The opening balance arrives as a string and is parsed with `Decimal`.** A JSON
number is a `float` by the time `json.loads` is done with it, and 10000.10 as a
binary float is not 10000.10; that error lands in `cash`, then in NAV on the
first fill applied on top of it, and NUMERIC storage cannot undo it afterwards.
`create_portfolio` refuses a `float` by `isinstance`, which is the right refusal
in the wrong place -- it would surface here as a 500 -- so the parse happens at
the gate, the same `Decimal(raw)` + `is_finite()` idiom `trading.py` uses for
query parameters.

The response is the shape `GET /trading/portfolio` returns, built from the same
`_position_payload` and `_cash_payload` helpers rather than a second rendering
of them, so a caller cannot read one field one way on creation and another way
on the next read.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from uuid import UUID

from neutron import App, Router
from neutron.error import bad_request, conflict, unauthorized
from pydantic import BaseModel
from starlette.requests import Request

from omni.api.trading import _cash_payload, _position_payload
from omni.auth import resolve_audience_from_request
from omni.portfolio.state import DuplicatePortfolio, create_portfolio


def _require_user(request: Request) -> UUID:
    """The caller's user id from a verified token, or a 401.

    A portfolio has no anonymous case: ``None`` means the caller is nobody, and
    a book owned by nobody is a row no audience-scoped read path can reach.
    """
    user = resolve_audience_from_request(request)
    if user is None:
        raise unauthorized("Authentication required")
    return user


def _opening_cash(raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise bad_request(f"opening_cash is not a number: {raw!r}") from exc
    if not value.is_finite():
        raise bad_request(f"opening_cash must be finite, got {raw!r}")
    return value


class CreatePortfolioIn(BaseModel):
    """The stated facts about a new book. No owner field, by construction.

    `opening_cash` is typed `str` so a JSON number is refused at validation
    rather than silently coerced through a float.
    """

    name: str
    base_currency: str
    cash_venue: str
    opening_cash: str


def build_router(app: App) -> Router:
    router = Router()

    @router.post("/portfolio")
    async def create(body: CreatePortfolioIn, request: Request) -> dict:
        user = _require_user(request)
        opening_cash = _opening_cash(body.opening_cash)

        try:
            book = await create_portfolio(
                app.db.pool,
                user_id=user,
                name=body.name,
                base_currency=body.base_currency,
                opening_cash=opening_cash,
                cash_venue=body.cash_venue,
            )
        except DuplicatePortfolio as exc:
            # 409 rather than 400: the request is well formed and the refusal is
            # about the state of the account, and rather than 200 on the
            # existing book, which would report a creation that did not happen.
            raise conflict(str(exc)) from exc
        except (ValueError, TypeError) as exc:
            # `create_portfolio`'s own refusals -- a blank name, a negative or
            # non-finite opening balance. They are the caller's mistakes and
            # must not read as the server's.
            raise bad_request(str(exc)) from exc

        return {
            "portfolio_id": str(book.portfolio_id),
            "as_of": book.as_of.isoformat(),
            "nav": str(book.nav),
            "cash": str(book.cash),
            "gross_exposure": str(book.gross_exposure),
            "net_exposure": str(book.net_exposure),
            "positions": [_position_payload(p) for p in book.positions],
            "cash_positions": [_cash_payload(c) for c in book.cash_positions],
        }

    return router


__all__ = ["build_router"]
