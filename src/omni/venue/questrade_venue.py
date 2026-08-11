"""Questrade venue adapter — equity execution for the Canadian market.

Implements the same Venue protocol as CCXTVenue, routed through Questrade's
REST API. This is what connects the scanner's "hold VTI" to an actual order.

**Auth flow:** Questrade uses OAuth 2.0 with refresh tokens. The operator
obtains an initial refresh token from Questrade's API portal (one-time manual
step). This adapter exchanges it for a short-lived access token (30 min),
which auto-refreshes. Each refresh also rotates the refresh token.

**Symbol model:** Questrade identifies instruments by numeric symbol IDs, not
tickers. This adapter caches the mapping (ticker -> symbolId) after the first
lookup. The asset name ("VTI") becomes a symbolId (e.g. 12345) internally.

**Account model:** Questrade supports multiple account types (margin, RRSP,
TFSA). The adapter defaults to the first margin account (which supports
shorting) but accepts an explicit account_id.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from omni.venue.protocol import (
    Balance,
    Capabilities,
    Fill,
    MarketType,
    OrderKind,
    Position,
    Quote,
    Side,
    TradeIntent,
    Venue,
    VenueUnavailable,
)

logger = logging.getLogger("omni.venue.questrade")

TOKEN_URL = "https://login.questrade.com/oauth2/token"
PRACTICE_TOKEN_URL = "https://practice-login.questrade.com/oauth2/token"

HttpFetcher = Callable[..., Awaitable[dict[str, Any]]]


class QuestradeVenue(Venue):
    """Equity venue backed by Questrade's REST API."""

    name = "questrade"

    def __init__(
        self,
        *,
        refresh_token: str,
        practice: bool = True,
        account_id: str | None = None,
        fetch_fn: HttpFetcher | None = None,
    ) -> None:
        self._refresh_token = refresh_token
        self._practice = practice
        self._account_id = account_id
        self._access_token: str | None = None
        self._token_expiry: datetime | None = None
        self._api_server: str | None = None
        self._symbol_cache: dict[str, int] = {}
        self._fetch = fetch_fn or self._default_fetch
        self._capabilities = Capabilities(
            spot=True,
            margin=True,
            perpetuals=False,
            limit_orders=True,
            shorting=True,
            funding_data=False,
            maker_fee_bps=Decimal(0),
            taker_fee_bps=Decimal(0),
            min_notional=Decimal(1),
        )

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    @classmethod
    async def connect(
        cls,
        *,
        refresh_token: str,
        practice: bool = True,
        account_id: str | None = None,
        fetch_fn: HttpFetcher | None = None,
    ) -> QuestradeVenue:
        venue = cls(
            refresh_token=refresh_token,
            practice=practice,
            account_id=account_id,
            fetch_fn=fetch_fn,
        )
        await venue._refresh_access_token()
        if account_id is None:
            await venue._resolve_account()
        logger.info(
            "questrade connected (practice=%s, account=%s)", practice, venue._account_id
        )
        return venue

    async def aclose(self) -> None:
        pass

    # --- Venue protocol ---

    def symbol_for(self, asset: str, market_type: MarketType) -> str | None:
        if market_type is not MarketType.SPOT and market_type is not MarketType.MARGIN:
            return None
        return asset

    async def quote(self, intent: TradeIntent) -> Quote:
        await self._ensure_token()
        symbol_id = await self._resolve_symbol(intent.symbol)
        path = f"{self._api_server}/v1/markets/quotes/{symbol_id}"
        resp = await self._fetch("GET", path, token=self._access_token)
        quotes = resp.get("quotes", [])
        if not quotes:
            raise VenueUnavailable(f"questrade returned no quote for {intent.symbol}")
        q = quotes[0]
        bid = _d(q.get("bidPrice"))
        ask = _d(q.get("askPrice"))
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            raise VenueUnavailable(f"questrade quote for {intent.symbol} has no bid/ask")
        mid = (bid + ask) / 2
        spread_bps = (ask - bid) / mid * Decimal(10000) if mid > 0 else Decimal(0)
        return Quote(
            intent=intent,
            expected_price=mid,
            fee=Decimal(0),
            slippage=spread_bps / 2,
            gas=Decimal(0),
            as_of=datetime.now(UTC),
        )

    async def execute(self, intent: TradeIntent) -> Fill:
        await self._ensure_token()
        symbol_id = await self._resolve_symbol(intent.symbol)
        order_format = self._build_order(intent, symbol_id)
        path = f"{self._api_server}/v1/accounts/{self._account_id}/orders"
        resp = await self._fetch("POST", path, token=self._access_token, json=order_format)
        order_id = resp.get("id")
        if order_id is None:
            raise VenueUnavailable(
                f"questrade order for {intent.symbol} returned no id: {resp}"
            )
        filled = await self._poll_order(order_id)
        return filled

    async def positions(self) -> list[Position]:
        await self._ensure_token()
        path = f"{self._api_server}/v1/accounts/{self._account_id}/positions"
        resp = await self._fetch("GET", path, token=self._access_token)
        positions: list[Position] = []
        for p in resp.get("positions", []):
            qty = _d(p.get("openQuantity")) or Decimal(0)
            if qty == 0:
                continue
            sym = p.get("symbol", "")
            entry = _d(p.get("averageEntryPrice")) or Decimal(0)
            positions.append(
                Position(
                    venue=self.name,
                    symbol=sym,
                    market_type=MarketType.MARGIN if qty < 0 else MarketType.SPOT,
                    quantity=qty,
                    average_entry=entry,
                    as_of=datetime.now(UTC),
                )
            )
        return positions

    async def balances(self) -> list[Balance]:
        await self._ensure_token()
        path = f"{self._api_server}/v1/accounts/{self._account_id}/balances"
        resp = await self._fetch("GET", path, token=self._access_token)
        balances: list[Balance] = []
        per_currency = resp.get("perCurrencyBalances", [])
        for b in per_currency:
            currency = b.get("currency", "CAD")
            cash = _d(b.get("cash")) or Decimal(0)
            if cash != 0:
                balances.append(
                    Balance(
                        venue=self.name,
                        asset=currency,
                        free=cash,
                        locked=Decimal(0),
                        as_of=datetime.now(UTC),
                    )
                )
        return balances

    async def cancel(self, external_id: str) -> bool:
        await self._ensure_token()
        path = f"{self._api_server}/v1/accounts/{self._account_id}/orders/{external_id}"
        try:
            await self._fetch("DELETE", path, token=self._access_token)
            return True
        except VenueUnavailable:
            return False

    # --- internals ---

    async def _default_fetch(
        self, method: str, url: str, *, token: str | None = None, json: dict | None = None,
    ) -> dict[str, Any]:
        import httpx

        headers: dict[str, str] = {"accept": "application/json"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.request(method, url, headers=headers, json=json)
        except httpx.HTTPError as exc:
            raise VenueUnavailable(f"questrade {method} {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise VenueUnavailable(
                f"questrade {method} {url} returned {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()

    async def _refresh_access_token(self) -> None:
        url = (PRACTICE_TOKEN_URL if self._practice else TOKEN_URL)
        path = f"{url}?grant_type=refresh_token&refresh_token={self._refresh_token}"
        resp = await self._fetch("POST", path)
        self._access_token = resp["access_token"]
        self._api_server = resp["api_server"].rstrip("/")
        expires_in = int(resp.get("expires_in", 1800))
        self._token_expiry = datetime.now(UTC) + timedelta(seconds=expires_in - 60)
        self._refresh_token = resp.get("refresh_token", self._refresh_token)
        logger.debug("questrade token refreshed, expires at %s", self._token_expiry)

    async def _ensure_token(self) -> None:
        if self._access_token and self._token_expiry and datetime.now(UTC) < self._token_expiry:
            return
        await self._refresh_access_token()

    async def _resolve_account(self) -> None:
        path = f"{self._api_server}/v1/accounts"
        resp = await self._fetch("GET", path, token=self._access_token)
        accounts = resp.get("accounts", [])
        margin = [a for a in accounts if a.get("type") == "Margin"]
        chosen = margin[0] if margin else accounts[0] if accounts else None
        if chosen is None:
            raise VenueUnavailable("questrade: no accounts found")
        self._account_id = str(chosen["number"])

    async def _resolve_symbol(self, ticker: str) -> int:
        if ticker in self._symbol_cache:
            return self._symbol_cache[ticker]
        path = f"{self._api_server}/v1/symbols/search?prefix={ticker}"
        resp = await self._fetch("GET", path, token=self._access_token)
        symbols = resp.get("symbols", [])
        exact = [s for s in symbols if s.get("symbol") == ticker]
        if not exact:
            raise VenueUnavailable(f"questrade: symbol {ticker} not found")
        symbol_id = int(exact[0]["symbolId"])
        self._symbol_cache[ticker] = symbol_id
        return symbol_id

    def _build_order(self, intent: TradeIntent, symbol_id: int) -> dict[str, Any]:
        side = "BuyAll" if intent.side is Side.BUY else "SellAll"
        order: dict[str, Any] = {
            "symbolId": symbol_id,
            "quantity": int(intent.quantity),
            "side": side,
            "orderType": "Market" if intent.order_kind is OrderKind.MARKET else "Limit",
            "timeInForce": "Day",
            "accountId": int(self._account_id),
        }
        if intent.order_kind is OrderKind.LIMIT:
            assert intent.limit_price is not None
            order["limitPrice"] = float(intent.limit_price)
        return order

    async def _poll_order(self, order_id: int, timeout: int = 10) -> Fill:
        import asyncio

        path = f"{self._api_server}/v1/accounts/{self._account_id}/orders/{order_id}"
        for _ in range(timeout):
            await asyncio.sleep(1)
            resp = await self._fetch("GET", path, token=self._access_token)
            orders = resp.get("orders", [])
            if not orders:
                continue
            o = orders[0]
            state = o.get("state", "")
            if state in ("Filled", "PartiallyFilled", "Rejected", "Cancelled"):
                break
        else:
            raise VenueUnavailable(
                f"questrade order {order_id} did not settle in {timeout}s"
            )

        filled_qty = _d(o.get("filledQuantity")) or Decimal(0)
        avg_price = _d(o.get("executionPrice")) or _d(o.get("limitPrice")) or Decimal(0)

        return Fill(
            intent_id="",
            venue=self.name,
            symbol=str(o.get("symbol", "")),
            side=Side.BUY if o.get("side") == "Buy" else Side.SELL,
            filled_quantity=filled_qty,
            average_price=avg_price,
            fee_paid=Decimal(0),
            filled_at=datetime.now(UTC),
            external_id=str(order_id),
            raw=o,
        )


def _d(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
