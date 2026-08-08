"""Opening a book over HTTP.

What is pinned here is where each value came from rather than what the response
looks like: the owner must be the authenticated principal and not a field the
caller supplied, the opening balance must survive as the exact decimal it was
written as and never as the nearest binary float, a second book under the same
name must be a refusal rather than an ambiguity, and every refusal must be the
caller's error rather than the server's. Every refusal case also asserts that
nothing was created, because a 4xx returned after a write is worse than a 200.
"""

import asyncio
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from neutron.auth.jwt import create_token
from neutron.test import TestClient

from omni.main import create_app

GOOD_SECRET = "x" * 48


class _Lifespan:
    def __init__(self, app):
        self._app = app
        self._receive = asyncio.Queue()
        self._send = asyncio.Queue()
        self._task = None

    async def __aenter__(self):
        self._task = asyncio.create_task(
            self._app({"type": "lifespan"}, self._receive.get, self._send.put)
        )
        await self._receive.put({"type": "lifespan.startup"})
        message = await self._send.get()
        assert message["type"] == "lifespan.startup.complete", message
        return self._app

    async def __aexit__(self, *exc):
        await self._receive.put({"type": "lifespan.shutdown"})
        await self._send.get()
        await self._task


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("OMNI_JWT_SECRET", GOOD_SECRET)
    yield


@pytest.fixture(autouse=True)
async def _clean_users(db):
    await db.pool.execute("TRUNCATE users CASCADE")
    yield


async def _operator(client) -> tuple[str, UUID]:
    """The first-run operator's token and the user id it authenticates as."""
    r = await client.post(
        "/auth/setup", json={"email": "op@example.com", "password": "a" * 16}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["token"], UUID(body["user"]["id"])


async def _second_user(client, token) -> UUID:
    r = await client.post(
        "/auth/register",
        json={"email": "other@example.com", "password": "b" * 16},
        headers={"authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    return UUID(r.json()["id"])


def _bearer(user_id: UUID) -> dict:
    return {"authorization": f"Bearer {create_token({'sub': str(user_id)}, GOOD_SECRET)}"}


def _body(**overrides) -> dict:
    body = {
        "name": f"book-{uuid4().hex[:8]}",
        "base_currency": "USD",
        "cash_venue": "binance",
        "opening_cash": "10000.10",
    }
    body.update(overrides)
    return body


async def _create(client, token, **overrides):
    return await client.post(
        "/portfolio",
        json=_body(**overrides),
        headers={"authorization": f"Bearer {token}"},
    )


async def _portfolio_count(db) -> int:
    return await db.pool.fetchval("SELECT count(*) FROM portfolio")


class TestAuthentication:
    async def test_an_anonymous_caller_is_refused_and_creates_nothing(
        self, db, database_url
    ):
        """401, and no row. A create path that refuses after writing has already
        opened the book the refusal claims it did not."""
        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            r = await client.post("/portfolio", json=_body())

        assert r.status_code == 401, r.text
        assert await _portfolio_count(db) == 0


class TestTheOwnerComesFromTheToken:
    async def test_a_body_supplied_owner_is_not_the_owner(self, db, database_url):
        """The leak this endpoint exists not to have.

        `portfolio.user_id` is the key every audience-scoped read path resolves
        on, so an owner taken from the body would let one account open a book
        owned by another and then read it through the other account. The book
        must belong to the token, and the named account must not be able to see
        it.
        """
        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, operator = await _operator(client)
            other = await _second_user(client, token)

            r = await _create(client, token, name="theirs", user_id=str(other))
            assert r.status_code == 201, r.text
            portfolio_id = UUID(r.json()["portfolio_id"])

            # The account the body named must not reach it.
            theirs = await client.get("/trading/portfolio", headers=_bearer(other))

        stored = await db.pool.fetchval(
            "SELECT user_id FROM portfolio WHERE id = $1", portfolio_id
        )
        assert stored == operator
        assert stored != other
        assert theirs.status_code == 404, theirs.text

    async def test_the_created_book_is_reachable_by_the_caller(self, db, database_url):
        """The other half of the same fact: a book whose owner was written
        correctly resolves through the audience-scoped read path without the
        caller naming an id. A NULL or wrong `user_id` is a row that exists and
        no operator can reach, which is indistinguishable from no row at all
        until someone funds it."""
        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, _ = await _operator(client)
            created = await _create(client, token, name="mine")
            assert created.status_code == 201, created.text

            read = await client.get(
                "/trading/portfolio", headers={"authorization": f"Bearer {token}"}
            )

        assert read.status_code == 200, read.text
        assert read.json()["portfolio_id"] == created.json()["portfolio_id"]


class TestOpeningCash:
    async def test_a_decimal_string_is_stored_exactly(self, db, database_url):
        """10000.10 is not representable in binary64. Parsed through a float it
        becomes 10000.0999999999994543031789362430572509765625, which NUMERIC
        then stores faithfully -- the error is permanent and lands in NAV on the
        first fill applied on top of it."""
        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, _ = await _operator(client)
            r = await _create(client, token, opening_cash="10000.10")
            assert r.status_code == 201, r.text
            body = r.json()
            portfolio_id = UUID(body["portfolio_id"])

        assert Decimal(body["cash"]) == Decimal("10000.10")
        assert Decimal(body["nav"]) == Decimal("10000.10")
        # And in the row, not only in the rendering of it.
        stored = await db.pool.fetchval(
            "SELECT free FROM cash_balance WHERE portfolio_id = $1", portfolio_id
        )
        assert stored == Decimal("10000.10")

    async def test_a_json_number_is_refused_and_creates_nothing(
        self, db, database_url
    ):
        """A JSON number is a float by the time json.loads is done with it. It
        is refused at the gate rather than converted, because there is no
        conversion that recovers the digits the float has already lost."""
        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, _ = await _operator(client)
            r = await client.post(
                "/portfolio",
                json={**_body(), "opening_cash": 10000.10},
                headers={"authorization": f"Bearer {token}"},
            )

        assert r.status_code in (400, 422), r.text
        assert await _portfolio_count(db) == 0

    async def test_an_unparseable_opening_cash_is_a_400(self, db, database_url):
        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, _ = await _operator(client)
            r = await _create(client, token, opening_cash="ten thousand")

        assert r.status_code == 400, r.text
        assert "opening_cash" in r.text
        assert await _portfolio_count(db) == 0

    @pytest.mark.parametrize("amount", ["nan", "Infinity", "-Infinity"])
    async def test_a_non_finite_opening_cash_is_a_400(
        self, db, database_url, amount
    ):
        """NUMERIC accepts 'NaN' and sorts it above every number, so a NaN
        opening balance would pass the schema and make every later comparison
        against the book's cash silently false."""
        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, _ = await _operator(client)
            r = await _create(client, token, opening_cash=amount)

        assert r.status_code == 400, r.text
        assert await _portfolio_count(db) == 0

    async def test_a_negative_opening_cash_is_a_400(self, db, database_url):
        """An opening balance is a deposit. A borrow against a venue is a fill,
        not a starting point."""
        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, _ = await _operator(client)
            r = await _create(client, token, opening_cash="-1")

        assert r.status_code == 400, r.text
        assert await _portfolio_count(db) == 0

    async def test_a_stated_zero_opens_an_unfunded_book(self, db, database_url):
        """Zero is accepted when it is stated -- a book waiting on a deposit is
        a real thing to want. What the endpoint refuses is arriving at zero by
        omission, which is why `opening_cash` has no default."""
        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, _ = await _operator(client)
            r = await _create(client, token, opening_cash="0")

        assert r.status_code == 201, r.text
        assert Decimal(r.json()["cash"]) == Decimal(0)


class TestRefusals:
    async def test_a_duplicate_name_is_a_conflict_and_not_a_second_book(
        self, db, database_url
    ):
        """409, and still one book.

        A second portfolio under one name is indistinguishable from the first at
        every call site that names a book by its name, and it turns the owner's
        unqualified `/trading/portfolio` from an answer into an ambiguity. A 500
        here would report the same refusal as a fault in the server, which is
        the one reading that invites a retry.
        """
        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, _ = await _operator(client)
            first = await _create(client, token, name="alpha")
            assert first.status_code == 201, first.text

            second = await _create(client, token, name="alpha")

        assert second.status_code == 409, second.text
        assert await _portfolio_count(db) == 1

    async def test_a_missing_field_is_refused_and_creates_nothing(
        self, db, database_url
    ):
        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, _ = await _operator(client)
            incomplete = _body()
            del incomplete["opening_cash"]
            r = await client.post(
                "/portfolio",
                json=incomplete,
                headers={"authorization": f"Bearer {token}"},
            )

        assert r.status_code in (400, 422), r.text
        assert await _portfolio_count(db) == 0

    @pytest.mark.parametrize(
        "field", ["name", "base_currency", "cash_venue"]
    )
    async def test_a_blank_field_is_a_400_not_a_500(self, db, database_url, field):
        """A whitespace name is a book nobody can name afterwards, and a blank
        cash venue puts real money at no location -- `reconcile` would then
        check the book against an account holding none of it."""
        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, _ = await _operator(client)
            r = await _create(client, token, **{field: "   "})

        assert r.status_code == 400, r.text
        assert field in r.text
        assert await _portfolio_count(db) == 0


class TestCreateAndReadAgree:
    async def test_the_created_payload_is_what_the_read_endpoint_returns(
        self, db, database_url
    ):
        """Two renderings of one book is how they come to disagree. The create
        response carries the contract's fields, produced by the same helpers the
        read endpoint uses, and every field but `as_of` -- which is read at the
        moment of reading -- must match what a subsequent read returns."""
        app = create_app(database_url)
        async with _Lifespan(app), TestClient(app) as client:
            token, _ = await _operator(client)
            created = await _create(client, token, opening_cash="2500.25")
            assert created.status_code == 201, created.text
            read = await client.get(
                "/trading/portfolio", headers={"authorization": f"Bearer {token}"}
            )
            assert read.status_code == 200, read.text

        left, right = created.json(), read.json()
        assert set(left) == {
            "portfolio_id",
            "as_of",
            "nav",
            "cash",
            "gross_exposure",
            "net_exposure",
            "positions",
            "cash_positions",
        }
        assert set(left) == set(right)
        for key in set(left) - {"as_of"}:
            assert left[key] == right[key], key

        # A book opened by a deposit holds nothing. Any exposure reported here
        # was derived from something other than a fill, which is the one thing
        # this payload is not allowed to contain.
        assert left["positions"] == []
        assert Decimal(left["gross_exposure"]) == Decimal(0)
        assert Decimal(left["net_exposure"]) == Decimal(0)

        # The opening cash is a real cash position at the venue that was stated,
        # not an unattributed balance: reconciliation matches by venue, and a
        # balance at no venue is one no venue can be checked against.
        assert [c["venue"] for c in left["cash_positions"]] == ["binance"]
        assert Decimal(left["cash_positions"][0]["free"]) == Decimal("2500.25")
        assert Decimal(left["cash_positions"][0]["locked"]) == Decimal(0)
