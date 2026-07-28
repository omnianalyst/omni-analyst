"""On-chain adapter: Etherscan flows/supply and DefiLlama TVL.

Fixtures are recorded excerpts of the real response shapes -- an Etherscan
`eth_getBlockByNumber` payload, a DefiLlama `/protocol/{slug}` payload, and
the composed supply payload (block timestamp + raw supply). The load-bearing
assertion across all three is that `knowledge_date == event_date`: on-chain
data is public the moment its block is mined, the cleanest bitemporal case
in the system, and it is asserted explicitly rather than left to arithmetic.
"""

from datetime import UTC, datetime

import pytest

from omni.ingest.onchain import OnChainAdapter, parse_flows, parse_supply, parse_tvl
from omni.ingest.protocol import Unavailable

# 1700000000 = 2023-11-14T22:13:20Z, hex 0x6553f100.
BLOCK_TS = 1700000000
BLOCK_TS_HEX = "0x6553f100"
MINED_AT = datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)

# wei hex values, computed once: 0.5 / 1 / 5 / 200 ETH.
WEI_HALF = "0x6f05b59d3b20000"
WEI_ONE = "0xde0b6b3a7640000"
WEI_FIVE = "0x4563918244f40000"
WEI_200 = "0xad78ebc5ac6200000"

BINANCE_14 = "0x28C6c06298d514Db089934071355E5743bf21d60"
COINBASE_1 = "0x71660c4005BA85c37ccec55d0C4493E66Fe775d3"
RANDOM_A = "0x01234567890abcdef01234567890abcdef01234567"
RANDOM_B = "0xfedcba9876543210fedcba9876543210fedcba98"

# Etherscan eth_getBlockByNumber excerpt. Mixed-case addresses exercise the
# case-insensitive exchange lookup. Four transactions: an exchange inflow, an
# exchange outflow, a whale transfer, and dust that must be dropped.
BLOCK_WITH_FLOWS = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "number": "0x1234567",
        "timestamp": BLOCK_TS_HEX,
        "transactions": [
            {
                "hash": "0xinflow1",
                "from": RANDOM_A,
                "to": BINANCE_14,
                "value": WEI_FIVE,
            },
            {
                "hash": "0xoutflow1",
                "from": COINBASE_1,
                "to": RANDOM_B,
                "value": WEI_ONE,
            },
            {
                "hash": "0xwhale1",
                "from": RANDOM_A,
                "to": RANDOM_B,
                "value": WEI_200,
            },
            {
                "hash": "0xdust1",
                "from": RANDOM_A,
                "to": RANDOM_B,
                "value": WEI_HALF,
            },
        ],
    },
}

# DefiLlama /protocol/{slug} excerpt. Two valid daily points and two that must
# be skipped (a null TVL, a null date) -- parsing must drop them, not guess.
TVL_PAYLOAD = {
    "name": "Uniswap",
    "tvl": [
        {"date": 1672531200, "totalLiquidityUSD": 1500000000.0},
        {"date": 1672617600, "totalLiquidityUSD": 1550000000.0},
        {"date": 1672704000, "tvl": None},
        {"date": None, "tvl": 999.0},
    ],
}

# 120000 ETH in wei (decimals=18) at block 0x6553f100.
SUPPLY_PAYLOAD = {
    "block_timestamp": BLOCK_TS_HEX,
    "supply": "120000000000000000000000",
}


def _at(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)


class TestParseFlows:
    def test_exchange_and_whale_transactions_become_flow_drafts(self):
        drafts = parse_flows(BLOCK_WITH_FLOWS)
        assert len(drafts) == 3
        by_hash = {d.key: d for d in drafts}
        assert set(by_hash) == {"0xinflow1", "0xoutflow1", "0xwhale1"}

    def test_dust_below_the_whale_threshold_is_dropped_not_guessed(self):
        drafts = parse_flows(BLOCK_WITH_FLOWS)
        assert all(d.key != "0xdust1" for d in drafts)

    def test_block_timestamp_is_the_event_date_never_now(self):
        drafts = parse_flows(BLOCK_WITH_FLOWS)
        assert {d.event_date for d in drafts} == {MINED_AT}

    def test_knowledge_date_equals_event_date_for_a_confirmed_block(self):
        # On-chain data is public the instant it is mined. Assert the equality
        # explicitly: computing it would let a future regression slip through.
        drafts = parse_flows(BLOCK_WITH_FLOWS)
        assert all(d.knowledge_date == d.event_date for d in drafts)
        assert all(d.knowledge_date == MINED_AT for d in drafts)

    def test_inflow_and_outflow_are_distinguished_by_direction(self):
        drafts = {d.key: d for d in parse_flows(BLOCK_WITH_FLOWS)}
        assert drafts["0xinflow1"].value["kind"] == "exchange_inflow"
        assert drafts["0xinflow1"].value["exchange"] == "Binance 14"
        assert drafts["0xinflow1"].value["amount_eth"] == 5.0
        assert drafts["0xoutflow1"].value["kind"] == "exchange_outflow"
        assert drafts["0xoutflow1"].value["exchange"] == "Coinbase 1"
        assert drafts["0xwhale1"].value["kind"] == "whale"
        assert drafts["0xwhale1"].value["exchange"] is None

    def test_the_claim_type_and_unit_are_onchain_flow_in_eth(self):
        for d in parse_flows(BLOCK_WITH_FLOWS):
            assert d.claim_type == "onchain_flow"
            assert d.unit == "ETH"

    def test_a_block_without_a_timestamp_yields_nothing_not_now(self):
        no_ts = {"result": {"timestamp": "0x0", "transactions": BLOCK_WITH_FLOWS["result"]["transactions"]}}
        assert parse_flows(no_ts) == []


class TestParseTvl:
    def test_each_daily_snapshot_becomes_a_tvl_draft(self):
        drafts = parse_tvl(TVL_PAYLOAD, slug="uniswap")
        assert len(drafts) == 2

    def test_snapshot_date_is_both_event_and_knowledge_date(self):
        drafts = parse_tvl(TVL_PAYLOAD, slug="uniswap")
        assert drafts[0].event_date == _at(1672531200)
        assert drafts[0].knowledge_date == drafts[0].event_date
        assert drafts[1].event_date == _at(1672617600)
        assert drafts[1].knowledge_date == drafts[1].event_date

    def test_the_slug_is_the_key_and_tvl_is_the_value(self):
        drafts = parse_tvl(TVL_PAYLOAD, slug="uniswap")
        assert {d.key for d in drafts} == {"uniswap"}
        assert {d.claim_type for d in drafts} == {"onchain_tvl"}
        assert drafts[0].value == {"tvl": 1500000000.0}

    def test_points_missing_a_date_or_tvl_are_skipped_not_zeroed(self):
        drafts = parse_tvl(
            {"tvl": [{"date": 1672531200, "totalLiquidityUSD": None},
                      {"date": None, "totalLiquidityUSD": 1.0}]},
            slug="x",
        )
        assert drafts == []


class TestParseSupply:
    def test_supply_is_human_and_raw_with_the_block_timestamp_as_dates(self):
        drafts = parse_supply(SUPPLY_PAYLOAD, token="ETH", decimals=18)
        assert len(drafts) == 1
        d = drafts[0]
        assert d.key == "ETH"
        assert d.claim_type == "onchain_supply"
        assert d.value["supply"] == 120000.0
        assert d.value["supply_raw"] == 120000000000000000000000
        assert d.value["decimals"] == 18
        assert d.event_date == MINED_AT
        assert d.knowledge_date == d.event_date

    def test_supply_never_introduces_a_price(self):
        d = parse_supply(SUPPLY_PAYLOAD, token="ETH", decimals=18)[0]
        # No USD, no rate, no market cap -- only the measured amount.
        assert "price" not in d.value
        assert "usd" not in d.value
        assert "market_cap" not in d.value

    def test_a_payload_missing_timestamp_or_supply_is_unfillable_not_zero(self):
        assert parse_supply({"supply": "123"}, token="ETH") == []
        assert parse_supply({"block_timestamp": BLOCK_TS_HEX}, token="ETH") == []


class TestAdapterRouting:
    async def test_flow_route_uses_an_injected_block_payload(self):
        async def fake(key: str):
            assert key == "flow:eth"
            return BLOCK_WITH_FLOWS

        drafts = await OnChainAdapter(fetch_fn=fake).fetch("flow:eth")
        assert len(drafts) == 3
        assert all(d.claim_type == "onchain_flow" for d in drafts)

    async def test_tvl_route_works_with_no_etherscan_key(self):
        # DefiLlama is keyless. An adapter with no api_key must still produce
        # TVL claims -- the TVL path never touches the Etherscan credential.
        async def fake(key: str):
            assert key == "tvl:uniswap"
            return TVL_PAYLOAD

        drafts = await OnChainAdapter(fetch_fn=fake).fetch("tvl:uniswap")
        assert len(drafts) == 2
        assert all(d.claim_type == "onchain_tvl" for d in drafts)

    async def test_supply_route_uses_an_injected_supply_payload(self):
        async def fake(key: str):
            assert key == "supply:ETH"
            return SUPPLY_PAYLOAD

        drafts = await OnChainAdapter(fetch_fn=fake).fetch("supply:ETH")
        assert len(drafts) == 1
        assert drafts[0].claim_type == "onchain_supply"

    async def test_an_empty_response_yields_no_drafts(self):
        async def empty(key: str):
            return {}

        for route in ("flow:eth", "tvl:uniswap", "supply:ETH"):
            assert await OnChainAdapter(fetch_fn=empty).fetch(route) == []

    async def test_a_fetch_failure_propagates_rather_than_returning_empty(self):
        async def broken(key: str):
            raise Unavailable("Etherscan returned HTTP 429")

        with pytest.raises(Unavailable, match="429"):
            await OnChainAdapter(fetch_fn=broken).fetch("flow:eth")


class TestAdapterCredentials:
    def test_the_adapter_declares_etherscan_for_licence_lookup(self):
        adapter = OnChainAdapter()
        assert adapter.source == "etherscan"
        assert adapter.provider_key == "etherscan"

    async def test_flow_without_a_key_or_fetcher_is_unavailable_not_empty(self):
        # An empty list would read as "no flows in this block"; the honest
        # answer is "we could not ask Etherscan".
        with pytest.raises(Unavailable, match="no Etherscan API key"):
            await OnChainAdapter().fetch("flow:eth")

    async def test_supply_without_a_key_or_fetcher_is_unavailable_not_empty(self):
        with pytest.raises(Unavailable, match="no Etherscan API key"):
            await OnChainAdapter().fetch("supply:ETH")

    async def test_a_malformed_key_is_unavailable(self):
        with pytest.raises(Unavailable, match="must be"):
            await OnChainAdapter().fetch("noColonHere")

    async def test_an_unknown_kind_is_unavailable(self):
        with pytest.raises(Unavailable, match="unknown onchain kind"):
            await OnChainAdapter().fetch("orbits:mars")


def test_the_tvl_fixture_matches_the_real_defillama_shape():
    """Guards the failure this fixture once had.

    The parser and its fixture were both written from memory as
    tvlHistory[].tvl, agreed with each other, and matched nothing DefiLlama
    serves. A green suite proved only that two guesses were consistent.
    """
    from omni.ingest import onchain

    # Behaviour, not source text: a real payload parses, the invented one does not.
    real = {"name": "Aave", "tvl": [{"date": 1589932800,
                                     "totalLiquidityUSD": 54026260}]}
    old = {"name": "Aave", "tvlHistory": [{"date": 1589932800, "tvl": 54026260}]}
    assert len(onchain.parse_tvl(real, slug="aave")) == 1
    assert onchain.parse_tvl(old, slug="aave") == []
