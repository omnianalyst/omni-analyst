import asyncio
import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import numpy as np
import pandas as pd
import pytest
from neutron.test import TestClient

from omni.api import risk_monitor
from omni.main import create_app

GOOD_SECRET = "r" * 48


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


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity, users CASCADE")
    yield


def _auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def _operator(client) -> tuple[str, UUID]:
    response = await client.post(
        "/auth/setup",
        json={"email": "alice@example.com", "password": "a" * 16},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body["token"], UUID(body["user"]["id"])


async def _member(client, operator_token: str) -> tuple[str, UUID]:
    response = await client.post(
        "/auth/register",
        json={"email": "bob@example.com", "password": "b" * 16},
        headers=_auth(operator_token),
    )
    assert response.status_code == 201, response.text
    user_id = UUID(response.json()["id"])
    response = await client.post(
        "/auth/login",
        json={"email": "bob@example.com", "password": "b" * 16},
    )
    assert response.status_code == 200, response.text
    return response.json()["token"], user_id


async def _portfolio(db, user_id: UUID) -> UUID:
    return await db.pool.fetchval(
        """
        INSERT INTO portfolio (user_id, name, base_currency)
        VALUES ($1, $2, 'USD') RETURNING id
        """,
        user_id,
        f"book-{uuid4().hex[:8]}",
    )


async def _positions(db, portfolio_id: UUID) -> None:
    await db.pool.executemany(
        """
        INSERT INTO position (
            portfolio_id, venue, symbol, market_type, quantity, average_entry
        ) VALUES ($1, 'ccxt', $2, 'spot', $3, $4)
        """,
        [
            (portfolio_id, "BTC/USDC", Decimal(1), Decimal(100)),
            (portfolio_id, "ETH/USDC", Decimal(-1), Decimal(200)),
        ],
    )


async def _private_prices(db, owner: UUID) -> None:
    start = datetime.now(UTC) - timedelta(days=30)
    for symbol in ("BTC", "ETH"):
        entity_id = await db.pool.fetchval(
            """
            INSERT INTO entity (kind, symbol, name)
            VALUES ('crypto', $1, $1) RETURNING id
            """,
            symbol,
        )
        values = []
        for day in range(21):
            observed = start + timedelta(days=day)
            close = 100 + day * 2 if symbol == "BTC" else 200 + day * day
            values.append(
                (
                    entity_id,
                    f"{symbol}/USDC",
                    json.dumps({"close": close}),
                    observed,
                    owner,
                )
            )
        await db.pool.executemany(
            """
            INSERT INTO claim (
                entity_id, claim_type, key, value, source, event_date,
                knowledge_date, confidence, credential_owner,
                redistributable, audience_user_id
            ) VALUES (
                $1, 'price_snapshot', $2, $3::jsonb, 'ccxt', $4,
                $4, 1.0, 'user', 'byo_only', $5
            )
            """,
            values,
        )


def _common_factor_returns(*assets: str) -> pd.DataFrame:
    factor = np.array([0.01, -0.015, 0.02, -0.005, 0.012] * 4)
    return pd.DataFrame({asset: factor for asset in assets})


def test_mismatched_btc_eth_notionals_are_factor_exposed():
    result = risk_monitor._pca_risk(
        _common_factor_returns("BTC", "ETH"),
        [
            {"symbol": "BTC/USDC", "market_type": "spot", "quantity": 1},
            {"symbol": "ETH/USDC", "market_type": "spot", "quantity": -1},
        ],
        pd.Series({"BTC": 60_000.0, "ETH": 3_000.0}),
    )

    expected_pc1_exposure = (60_000.0 - 3_000.0) / math.sqrt(2) / 63_000.0
    assert result["verdict"] == "factor_exposed"
    assert result["pc1_exposure"] == pytest.approx(expected_pc1_exposure, abs=5e-5)
    assert result["net_ratio"] == pytest.approx(57_000 / 63_000, abs=5e-5)
    assert result["notionals"] == {"BTC": 60_000.0, "ETH": -3_000.0}
    assert result["gross_notional"] == 63_000.0


def test_balanced_same_asset_pair_is_delta_neutral():
    result = risk_monitor._pca_risk(
        _common_factor_returns("BTC"),
        [
            {"symbol": "BTC/USDC", "market_type": "spot", "quantity": 1},
            {
                "symbol": "BTC/USDC:USDC",
                "market_type": "perpetual",
                "quantity": -1,
            },
        ],
        pd.Series({"BTC": 60_000.0}),
    )

    assert result["verdict"] == "delta_neutral"
    assert result["pc1_exposure"] == 0.0
    assert result["net_ratio"] == 0.0
    assert result["notionals"] == {"BTC": 0.0}
    assert result["gross_notional"] == 120_000.0


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf")])
def test_non_finite_return_history_refuses_a_verdict(bad_value):
    returns = _common_factor_returns("BTC", "ETH")
    returns.loc[5, "BTC"] = bad_value

    result = risk_monitor._pca_risk(
        returns,
        [{"symbol": "BTC/USDC", "market_type": "spot", "quantity": 1}],
        pd.Series({"BTC": 60_000.0, "ETH": 3_000.0}),
    )

    assert result == {
        "verdict": "insufficient_data",
        "detail": "Return history contains non-finite values",
    }


def test_zero_variance_return_history_refuses_a_verdict():
    returns = _common_factor_returns("BTC", "ETH")
    returns["ETH"] = 0.01

    result = risk_monitor._pca_risk(
        returns,
        [
            {"symbol": "BTC/USDC", "market_type": "spot", "quantity": 1},
            {"symbol": "ETH/USDC", "market_type": "spot", "quantity": -1},
        ],
        pd.Series({"BTC": 60_000.0, "ETH": 3_000.0}),
    )

    assert result == {
        "verdict": "insufficient_data",
        "detail": "PCA requires non-zero return variance for every asset",
    }


@pytest.mark.parametrize("bad_price", [0.0, float("nan"), float("inf")])
def test_invalid_point_in_time_price_refuses_a_verdict(bad_price):
    result = risk_monitor._pca_risk(
        _common_factor_returns("BTC", "ETH"),
        [
            {"symbol": "BTC/USDC", "market_type": "spot", "quantity": 1},
            {"symbol": "ETH/USDC", "market_type": "spot", "quantity": -1},
        ],
        pd.Series({"BTC": bad_price, "ETH": 3_000.0}),
    )

    assert result == {
        "verdict": "insufficient_data",
        "detail": "Current prices must be finite and positive for every asset",
    }


async def test_anonymous_and_inactive_callers_cannot_use_cache_or_compute(
    db, database_url, monkeypatch
):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        token, user_id = await _operator(client)
        portfolio_id = await _portfolio(db, user_id)
        primed = await client.get("/scanner/risk", headers=_auth(token))
        assert primed.status_code == 200, primed.text
        assert primed.json()["verdict"] == "flat"

        calls = []

        async def unexpected(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("authorization must precede portfolio and risk work")

        monkeypatch.setattr(risk_monitor, "_resolve_portfolio", unexpected)
        monkeypatch.setattr(risk_monitor, "_assess", unexpected)

        anonymous = await client.get("/scanner/risk")
        await db.pool.execute("UPDATE users SET active = false WHERE id = $1", user_id)
        inactive = await client.get(
            f"/scanner/risk?portfolio_id={portfolio_id}", headers=_auth(token)
        )

    assert anonymous.status_code == 401
    assert inactive.status_code == 401
    assert calls == []


async def test_account_without_a_portfolio_gets_404_before_assessment(
    database_url, monkeypatch
):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        token, _ = await _operator(client)
        assessed = False

        async def unexpected(*args, **kwargs):
            nonlocal assessed
            assessed = True
            raise AssertionError("a missing portfolio must not be assessed")

        monkeypatch.setattr(risk_monitor, "_assess", unexpected)
        response = await client.get("/scanner/risk", headers=_auth(token))

    assert response.status_code == 404
    assert "No portfolio for this account" in response.text
    assert assessed is False


async def test_foreign_portfolio_id_is_reported_missing_without_assessment(
    db, database_url, monkeypatch
):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        alice_token, _ = await _operator(client)
        _, bob_id = await _member(client, alice_token)
        bob_portfolio = await _portfolio(db, bob_id)
        await _positions(db, bob_portfolio)
        assessed = False

        async def unexpected(*args, **kwargs):
            nonlocal assessed
            assessed = True
            raise AssertionError("a foreign portfolio must not be assessed")

        monkeypatch.setattr(risk_monitor, "_assess", unexpected)
        response = await client.get(
            f"/scanner/risk?portfolio_id={bob_portfolio}",
            headers=_auth(alice_token),
        )

    assert response.status_code == 404
    assert "BTC/USDC" not in response.text
    assert assessed is False


async def test_cross_user_ccxt_claims_do_not_supply_price_history(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        alice_token, alice_id = await _operator(client)
        bob_token, bob_id = await _member(client, alice_token)
        bob_portfolio = await _portfolio(db, bob_id)
        await _positions(db, bob_portfolio)
        await _private_prices(db, alice_id)

        response = await client.get("/scanner/risk", headers=_auth(bob_token))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "verdict": "insufficient_data",
        "detail": "Need 20+ days of price history; have 0",
        "positions": 2,
    }


async def test_own_ccxt_claims_supply_price_history(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        token, user_id = await _operator(client)
        portfolio_id = await _portfolio(db, user_id)
        await _positions(db, portfolio_id)
        await _private_prices(db, user_id)

        response = await client.get("/scanner/risk", headers=_auth(token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["portfolio_id"] == str(portfolio_id)
    assert body["n_assets"] == 2
    assert body["n_days"] == 20


async def test_requests_are_isolated_by_audience_and_selected_portfolio(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        alice_token, alice_id = await _operator(client)
        bob_token, bob_id = await _member(client, alice_token)
        flat_portfolio = await _portfolio(db, alice_id)
        risk_portfolio = await _portfolio(db, alice_id)
        await _positions(db, risk_portfolio)
        await _private_prices(db, alice_id)

        flat = await client.get(
            f"/scanner/risk?portfolio_id={flat_portfolio}",
            headers=_auth(alice_token),
        )
        risk = await client.get(
            f"/scanner/risk?portfolio_id={risk_portfolio}",
            headers=_auth(alice_token),
        )
        await db.pool.execute(
            "UPDATE portfolio SET user_id = $1 WHERE id = $2",
            bob_id,
            risk_portfolio,
        )
        transferred = await client.get(
            f"/scanner/risk?portfolio_id={risk_portfolio}",
            headers=_auth(bob_token),
        )

    assert flat.status_code == 200, flat.text
    assert flat.json()["verdict"] == "flat"
    assert risk.status_code == 200, risk.text
    assert risk.json()["portfolio_id"] == str(risk_portfolio)
    assert risk.json()["n_days"] == 20
    assert transferred.status_code == 200, transferred.text
    assert transferred.json()["verdict"] == "insufficient_data"
    assert transferred.json()["detail"].endswith("have 0")


async def test_changed_live_book_is_not_hidden_by_a_cached_flat_result(db, database_url):
    app = create_app(database_url)
    async with _Lifespan(app), TestClient(app) as client:
        token, user_id = await _operator(client)
        portfolio_id = await _portfolio(db, user_id)
        flat = await client.get("/scanner/risk", headers=_auth(token))

        await _positions(db, portfolio_id)
        await _private_prices(db, user_id)
        changed = await client.get("/scanner/risk", headers=_auth(token))

    assert flat.json()["verdict"] == "flat"
    assert changed.json()["portfolio_id"] == str(portfolio_id)
    assert changed.json()["n_days"] == 20
