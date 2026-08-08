"""DefiLlama adapter: fees, revenue, stablecoin supply, chain TVL.

Fixtures are recorded excerpts of real DefiLlama responses, captured from the
endpoints named on each constant. The load-bearing facts across all four kinds:

- fees and revenue come from SEPARATE responses (selected by the `dataType`
  query param) and are different numbers -- for Lido on 2021-04-30 DefiLlama
  reports fees=155000 and revenue=7750. Conflating them is the bug this module
  exists to prevent, so the distinction is asserted on real captured values.
- the snapshot date is both `event_date` and `knowledge_date`, asserted
  explicitly rather than left to arithmetic (same bitemporal case as TVL).
- a zero-value day (Lido had real zero-fee days; Ethereum had real zero-TVl
  days) is a valid observation and is emitted, never dropped.
"""

from datetime import UTC, datetime

import pytest

from omni.ingest import defillama
from omni.ingest.defillama import (
    DefiLlamaAdapter,
    parse_chain_tvl,
    parse_fees,
    parse_revenue,
    parse_stablecoin,
)
from omni.ingest.protocol import Unavailable


def _at(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)


async def _always_empty(key: str):
    return {}


# Recorded excerpt of https://api.llama.fi/summary/fees/lido?dataType=dailyFees
# (positional [unix_seconds, total] pairs under totalDataChart). Includes two
# real zero-fee days (2022-11-09, 2022-11-11).
LIDO_FEES_PAYLOAD = {
    "id": "lido",
    "name": "Lido",
    "totalDataChart": [
        [1619740800, 155000],
        [1619827200, 161262],
        [1667952000, 0],
        [1668124800, 0],
    ],
}

# Recorded excerpt of https://api.llama.fi/summary/fees/lido?dataType=dailyRevenue
# Same dates as the fees payload, but the protocol's own share -- an order of
# magnitude smaller (7750 vs 155000 on 2021-04-30).
LIDO_REVENUE_PAYLOAD = {
    "id": "lido",
    "name": "Lido",
    "totalDataChart": [
        [1619740800, 7750],
        [1619827200, 8063],
    ],
}

# Recorded excerpt of https://stablecoins.llama.fi/stablecoin/2 (USDC). The
# series lives in `tokens`; each point's supply is nested under the coin's own
# `pegType` key inside `circulating`.
USDC_STABLECOIN_PAYLOAD = {
    "id": "2",
    "name": "USD Coin",
    "symbol": "USDC",
    "pegType": "peggedUSD",
    "tokens": [
        {"date": 1536624000, "circulating": {"peggedUSD": 2}},
        {"date": 1545264000, "circulating": {"peggedUSD": 222892827}},
    ],
}

# Recorded excerpt of
# https://api.llama.fi/v2/historicalChainTvl/Ethereum (root JSON array of
# {date, tvl}). Early Ethereum days really had tvl=0.
ETH_CHAIN_TVL_PAYLOAD = [
    {"date": 1506470400, "tvl": 0},
    {"date": 1506556800, "tvl": 0},
    {"date": 1786060800, "tvl": 41655886906},
]


class TestEachKindEmitsItsClaimType:
    def test_fees_become_protocol_fees(self):
        drafts = parse_fees(LIDO_FEES_PAYLOAD, slug="lido")
        assert len(drafts) == 4
        assert {d.claim_type for d in drafts} == {"protocol_fees"}
        assert {d.key for d in drafts} == {"lido"}
        assert all(d.unit == "USD" for d in drafts)

    def test_revenue_becomes_protocol_revenue(self):
        drafts = parse_revenue(LIDO_REVENUE_PAYLOAD, slug="lido")
        assert {d.claim_type for d in drafts} == {"protocol_revenue"}

    def test_stablecoin_becomes_stablecoin_supply(self):
        drafts = parse_stablecoin(USDC_STABLECOIN_PAYLOAD, asset_id="2")
        assert len(drafts) == 2
        assert {d.claim_type for d in drafts} == {"stablecoin_supply"}
        assert {d.key for d in drafts} == {"2"}

    def test_chain_becomes_chain_tvl(self):
        drafts = parse_chain_tvl(ETH_CHAIN_TVL_PAYLOAD, chain="Ethereum")
        assert len(drafts) == 3
        assert {d.claim_type for d in drafts} == {"chain_tvl"}
        assert {d.key for d in drafts} == {"Ethereum"}


class TestFeesVsRevenue:
    def test_fees_and_revenue_are_different_numbers_for_the_same_day(self):
        fees = parse_fees(LIDO_FEES_PAYLOAD, slug="lido")
        rev = parse_revenue(LIDO_REVENUE_PAYLOAD, slug="lido")
        when = _at(1619740800)
        fee_on_day = next(d.value["fees"] for d in fees if d.event_date == when)
        rev_on_day = next(d.value["revenue"] for d in rev if d.event_date == when)
        # Real captured Lido values: 155000 fees vs 7750 revenue.
        assert fee_on_day == 155000.0
        assert rev_on_day == 7750.0
        assert fee_on_day != rev_on_day

    def test_the_two_claim_types_are_distinct(self):
        # A P/F ratio must read protocol_fees; if revenue were emitted under the
        # same claim type the ratio would silently use the wrong number.
        assert parse_fees(LIDO_FEES_PAYLOAD, slug="lido")[0].claim_type == ("protocol_fees")
        assert parse_revenue(LIDO_REVENUE_PAYLOAD, slug="lido")[0].claim_type == (
            "protocol_revenue"
        )

    def test_revenue_is_not_silently_served_as_fees(self):
        # If the revenue parser put its value under the fees key (the conflation
        # bug), this would raise KeyError; if it copied the fees number, the
        # assertion fails. The value must come from the revenue payload.
        rev = parse_revenue(LIDO_REVENUE_PAYLOAD, slug="lido")
        assert rev[0].value == {"revenue": 7750.0}
        assert "fees" not in rev[0].value


class TestBitemporalDates:
    def test_event_date_equals_knowledge_date_for_every_kind(self):
        all_drafts = [
            *parse_fees(LIDO_FEES_PAYLOAD, slug="lido"),
            *parse_revenue(LIDO_REVENUE_PAYLOAD, slug="lido"),
            *parse_stablecoin(USDC_STABLECOIN_PAYLOAD, asset_id="2"),
            *parse_chain_tvl(ETH_CHAIN_TVL_PAYLOAD, chain="Ethereum"),
        ]
        assert all(d.knowledge_date == d.event_date for d in all_drafts)

    def test_snapshot_dates_are_the_real_defillama_dates_not_now(self):
        fees = parse_fees(LIDO_FEES_PAYLOAD, slug="lido")
        assert fees[0].event_date == datetime(2021, 4, 30, tzinfo=UTC)
        assert fees[0].knowledge_date == datetime(2021, 4, 30, tzinfo=UTC)


class TestZeroValueDaysAreEmitted:
    def test_a_zero_fee_day_is_emitted_not_dropped(self):
        drafts = parse_fees(LIDO_FEES_PAYLOAD, slug="lido")
        zero_day = [d for d in drafts if d.event_date == datetime(2022, 11, 9, tzinfo=UTC)]
        assert len(zero_day) == 1
        assert zero_day[0].value["fees"] == 0.0

    def test_a_zero_tvl_day_is_emitted_not_dropped(self):
        drafts = parse_chain_tvl(ETH_CHAIN_TVL_PAYLOAD, chain="Ethereum")
        zero_days = [d for d in drafts if d.value["tvl"] == 0.0]
        assert len(zero_days) == 2

    def test_a_zero_supply_is_a_real_observation(self):
        payload = {
            "pegType": "peggedUSD",
            "tokens": [{"date": 1536624000, "circulating": {"peggedUSD": 0}}],
        }
        drafts = parse_stablecoin(payload, asset_id="2")
        assert len(drafts) == 1
        assert drafts[0].value["supply"] == 0.0


class TestMalformedPoints:
    def test_a_chart_point_missing_date_or_value_is_skipped(self):
        payload = {
            "name": "X",
            "totalDataChart": [
                [1619740800, 100],
                [None, 200],
                [1619827200, None],
                [1619913600, 300],
            ],
        }
        drafts = parse_fees(payload, slug="x")
        # The two malformed points are dropped; both valid neighbours survive.
        assert len(drafts) == 2
        assert [d.value["fees"] for d in drafts] == [100.0, 300.0]

    def test_a_non_array_chart_point_is_skipped(self):
        payload = {"totalDataChart": [[1619740800, 1], "garbage", [1619827200, 2]]}
        drafts = parse_fees(payload, slug="x")
        assert [d.value["fees"] for d in drafts] == [1.0, 2.0]

    def test_a_stablecoin_point_missing_circulating_is_skipped(self):
        payload = {
            "pegType": "peggedUSD",
            "tokens": [
                {"date": 1536624000, "circulating": {"peggedUSD": 5}},
                {"date": 1536700000},
                {"date": 1536710400, "circulating": {}},
                {"date": 1536800000, "circulating": {"peggedUSD": 9}},
            ],
        }
        drafts = parse_stablecoin(payload, asset_id="2")
        assert [d.value["supply"] for d in drafts] == [5.0, 9.0]


class TestFailurePaths:
    async def test_an_unknown_kind_raises_unavailable(self):
        async def fake(key):
            return {}

        with pytest.raises(Unavailable, match="unknown defillama kind"):
            await DefiLlamaAdapter(fetch_fn=fake).fetch("orbits:mars")

    async def test_a_key_without_a_colon_raises_unavailable(self):
        with pytest.raises(Unavailable, match="must be"):
            await DefiLlamaAdapter(fetch_fn=_always_empty).fetch("noColon")

    async def test_http_500_raises_unavailable(self, monkeypatch):
        class _Resp:
            status_code = 500

            def json(self):
                return {}

        async def fake_get_json(client, url, *, params=None, headers=None):
            return _Resp()

        monkeypatch.setattr(defillama, "get_json", fake_get_json)
        with pytest.raises(Unavailable, match="500"):
            await defillama._fetch_dimension("lido", "dailyFees")

    async def test_http_500_on_chain_tvl_raises_unavailable(self, monkeypatch):
        class _Resp:
            status_code = 500

            def json(self):
                return []

        async def fake_get_json(client, url, *, params=None, headers=None):
            return _Resp()

        monkeypatch.setattr(defillama, "get_json", fake_get_json)
        with pytest.raises(Unavailable, match="500"):
            await defillama._fetch_chain_tvl("Ethereum")

    async def test_an_empty_series_returns_empty_without_raising(self):
        async def empty_dict(key):
            return {"totalDataChart": []}

        async def empty_list(key):
            return []

        async def blank(key):
            return {}

        assert await DefiLlamaAdapter(fetch_fn=empty_dict).fetch("fees:x") == []
        assert await DefiLlamaAdapter(fetch_fn=empty_dict).fetch("revenue:x") == []
        assert await DefiLlamaAdapter(fetch_fn=blank).fetch("stablecoin:2") == []
        assert await DefiLlamaAdapter(fetch_fn=empty_list).fetch("chain:Ethereum") == []

    async def test_a_fetch_failure_propagates_not_swallowed(self):
        async def broken(key):
            raise Unavailable("DefiLlama returned HTTP 503 for fees/lido")

        with pytest.raises(Unavailable, match="503"):
            await DefiLlamaAdapter(fetch_fn=broken).fetch("fees:lido")


class TestAdapterRouting:
    def test_the_adapter_declares_defillama_for_licence_lookup(self):
        adapter = DefiLlamaAdapter()
        assert adapter.source == "defillama"
        assert adapter.provider_key == "defillama"

    async def test_each_kind_routes_to_its_parser(self):
        async def fake(key):
            return {
                "fees:lido": LIDO_FEES_PAYLOAD,
                "revenue:lido": LIDO_REVENUE_PAYLOAD,
                "stablecoin:2": USDC_STABLECOIN_PAYLOAD,
                "chain:Ethereum": ETH_CHAIN_TVL_PAYLOAD,
            }[key]

        fees = await DefiLlamaAdapter(fetch_fn=fake).fetch("fees:lido")
        rev = await DefiLlamaAdapter(fetch_fn=fake).fetch("revenue:lido")
        sc = await DefiLlamaAdapter(fetch_fn=fake).fetch("stablecoin:2")
        chain = await DefiLlamaAdapter(fetch_fn=fake).fetch("chain:Ethereum")

        assert all(d.claim_type == "protocol_fees" for d in fees)
        assert all(d.claim_type == "protocol_revenue" for d in rev)
        assert all(d.claim_type == "stablecoin_supply" for d in sc)
        assert all(d.claim_type == "chain_tvl" for d in chain)
        # sanity: a real value from each
        assert fees[0].value["fees"] == 155000.0
        assert rev[0].value["revenue"] == 7750.0
        assert sc[-1].value["supply"] == 222892827.0
        assert chain[-1].value["tvl"] == 41655886906.0


# --- Gate finding: the default fetch route was untested --------------------


class TestTheDefaultRouteRequestsTheRightDimension:
    """`revenue:` must reach `dailyRevenue`, and `fees:` must reach `dailyFees`.

    Every other test in this file injects `fetch_fn`, so the branch that picks
    the dimension never runs. That left the single most consequential mistake in
    the module invisible: pointing the revenue route at the fees dimension
    passed all 23 tests while emitting fees, labelled as revenue, into a
    `protocol_revenue` claim.

    Nothing downstream can recover from that. Fees are what users paid; revenue
    is the protocol's own share, and the two differ by whatever goes to
    liquidity providers -- frequently an order of magnitude. A P/F ratio built
    on the wrong one is not noisy, it is wrong, and it looks entirely healthy.

    So this asserts on the request rather than the response: what dimension did
    the adapter actually ask for.
    """

    async def _dimension_requested(self, monkeypatch, key: str) -> str:
        seen: dict[str, str] = {}

        async def fake_get_json(client, url, *, params=None, headers=None):
            seen["url"] = url
            seen["data_type"] = (params or {}).get("dataType", "")

            class Response:
                status_code = 200

                @staticmethod
                def json():
                    return {"totalDataChart": []}

            return Response()

        monkeypatch.setattr("omni.ingest.defillama.get_json", fake_get_json)
        await DefiLlamaAdapter().fetch(key)
        return seen["data_type"]

    async def test_the_revenue_route_asks_for_dailyRevenue(self, monkeypatch):
        assert await self._dimension_requested(monkeypatch, "revenue:lido") == (
            "dailyRevenue"
        )

    async def test_the_fees_route_asks_for_dailyFees(self, monkeypatch):
        assert await self._dimension_requested(monkeypatch, "fees:lido") == "dailyFees"

    async def test_the_two_routes_do_not_request_the_same_dimension(self, monkeypatch):
        fees = await self._dimension_requested(monkeypatch, "fees:lido")
        revenue = await self._dimension_requested(monkeypatch, "revenue:lido")
        assert fees != revenue
