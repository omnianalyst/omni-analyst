"""The extracted-capability registry: bound, honest, and planner-discoverable."""

import pytest

from omni.capability.builtin import build_builtin_registry
from omni.capability.extracted import CLAIM_TYPES, build_extracted_registry
from omni.capability.registry import Registry
from omni.ingest.protocol import Unavailable

# The schema enum, as declared in migrations/001_core_schema.sql and
# migrations/003_domains_and_graph.sql. Mirrored here so the test catches a
# capability (or a migration edit) that drifts outside it.
SCHEMA_CLAIM_TYPES = frozenset(
    {
        "price_snapshot",
        "fundamental_metric",
        "filing_event",
        "macro_series_point",
        "news_event",
        "manipulation_signal",
        "perception_news",
        "perception_macro",
        "perception_social",
        "perception_positioning",
        "perception_divergence",
        "onchain_flow",
        "onchain_tvl",
        "onchain_supply",
    }
)


@pytest.fixture
def registry():
    return build_extracted_registry()


# --- Small fixtures and the behaviour each cap must produce on them. --------
#
# Each entry is (kwargs, predicate). The predicate asserts a hand-computed
# value where one is tractable and otherwise a characteristic field, so this is
# a binding smoke-test (the deep maths lives in tests/test_cap_*.py, which this
# work order does not own).

_FIXTURE_DAILY = [0.01, -0.01] * 10  # 20 balanced returns


async def _stocktwits_fetcher(symbol):
    return {
        "messages": [
            {"entities": {"sentiment": {"basic": "Bullish"}}},
            {"entities": {"sentiment": {"basic": "Bullish"}}},
        ],
        "symbol": {"watchlist_count": 5000},
    }


def _cases():
    cpi = [100.0] * 11 + [101.0, 103.0]
    return {
        "macro.sahm_rule": (
            {"unemployment_values": [3.5] * 9 + [3.9, 4.1, 4.3]},
            lambda r: bool(r["triggered"]) is True
            and r["value"] == pytest.approx(0.6),
        ),
        "macro.yield_curve_inversion": (
            {
                "series_2y": {"2024-01": 4.0, "2024-02": 4.1},
                "series_10y": {"2024-01": 4.2, "2024-02": 4.3},
            },
            lambda r: r["current_spread"] == pytest.approx(0.2)
            and r["is_inverted"] is False,
        ),
        "macro.recession_probability": (
            {
                "yield_curve_inverted": True,
                "sahm_triggered": True,
                "lei_signals": ["negative", "negative"],
            },
            lambda r: r["probability"] == 1.0 and r["assessment"] == "high",
        ),
        "macro.inflation_measures": (
            {"cpi_values": cpi},
            lambda r: r["yoy"] == pytest.approx(3.0),
        ),
        "macro.pce_inflation": (
            {"pce_values": [100.0] * 12 + [102.0]},
            lambda r: r["yoy"] == pytest.approx(2.0)
            and r["vs_target"] == pytest.approx(0.0),
        ),
        "macro.inflation_expectations": (
            {"exp_5y": [2.2], "exp_10y": [2.3]},
            lambda r: r["5y5y_forward"] == pytest.approx(2.4)
            and r["anchored"] is True,
        ),
        "macro.labor_market_tightness": (
            {"unemployment_rate": 3.5, "job_growth_3m_avg": 200000.0},
            lambda r: r["score"] == pytest.approx((1 / 3.5) * 2.0, rel=1e-3)
            and r["assessment"] == "tight",
        ),
        "macro.taylor_rule": (
            {"inflation": 2.0, "output_gap": 0.0},
            lambda r: r == pytest.approx(2.5),
        ),
        "macro.taylor_rule_variant": (
            {
                "inflation": 2.0,
                "output_gap": 0.0,
                "target": 2.0,
                "r_star": 0.5,
                "alpha": 1.5,
                "beta": 0.5,
            },
            lambda r: r == pytest.approx(2.5),
        ),
        "macro.assess_policy_implications": (
            {"taylor_rate": 4.0, "current_rate": 2.0, "stance": "accommodative"},
            lambda r: r["rate_adjustment_needed"] == pytest.approx(2.0)
            and r["recommendation"]
            and "Inflation risk" in r["risks"][0],
        ),
        "macro.assess_scenario_impact": (
            {
                "baseline": {"gdp": {"2024": 2.0}},
                "scenario": {"gdp": {"2024": 2.5}},
                "variables": ["gdp"],
            },
            lambda r: r["gdp"]["average_impact"] == pytest.approx(0.5),
        ),
        "fundamentals.financial_ratios": (
            {
                "fundamentals": {
                    "income_statement": {
                        "eps": 10.0,
                        "earnings_growth_rate": 0.10,
                        "net_income": 1000.0,
                        "revenue": 5000.0,
                        "operating_income": 1500.0,
                        "cost_of_revenue": 2000.0,
                        "dividends_per_share": 2.0,
                    },
                    "balance_sheet": {
                        "book_value_per_share": 50.0,
                        "total_equity": 5000.0,
                        "total_assets": 10000.0,
                        "total_debt": 2000.0,
                        "current_assets": 3000.0,
                        "current_liabilities": 1500.0,
                        "inventory": 500.0,
                    },
                    "cash_flow": {
                        "operating_cash_flow": 1200.0,
                        "capital_expenditures": 200.0,
                    },
                },
                "current_price": 100.0,
            },
            lambda r: r["pe_ratio"] == pytest.approx(10.0),
        ),
        "fundamentals.dcf_valuation": (
            {
                "fundamentals": {
                    "income_statement": {},
                    "balance_sheet": {
                        "total_debt": 2000.0,
                        "shares_outstanding": 100.0,
                    },
                    "cash_flow": {
                        "operating_cash_flow": 1200.0,
                        "capital_expenditures": 200.0,
                    },
                },
                "current_price": 100.0,
                "growth_rate": 0.10,
                "discount_rate": 0.10,
            },
            lambda r: isinstance(r["fair_value_per_share"], float)
            and r["fair_value_per_share"] > 0,
        ),
        "fundamentals.peer_comparison": (
            {
                "symbol": "AAPL",
                "industry": "Tech",
                "sector": "Technology",
                "comparison_data": [
                    {
                        "symbol": "AAPL",
                        "is_target": True,
                        "pe_ratio": 25.0,
                        "peg_ratio": 1.5,
                        "pb_ratio": 4.0,
                        "roe": 30.0,
                        "debt_to_equity": 1.5,
                        "current_ratio": 1.0,
                        "gross_margin": 40.0,
                        "net_margin": 20.0,
                        "dividend_yield": 1.0,
                    },
                    {
                        "symbol": "PEER1",
                        "pe_ratio": 20.0,
                        "peg_ratio": 1.2,
                        "pb_ratio": 3.0,
                        "roe": 15.0,
                        "debt_to_equity": 0.5,
                        "current_ratio": 2.0,
                        "gross_margin": 35.0,
                        "net_margin": 10.0,
                        "dividend_yield": 2.0,
                    },
                    {
                        "symbol": "PEER2",
                        "pe_ratio": 30.0,
                        "peg_ratio": 1.8,
                        "pb_ratio": 5.0,
                        "roe": 25.0,
                        "debt_to_equity": 1.0,
                        "current_ratio": 1.5,
                        "gross_margin": 45.0,
                        "net_margin": 15.0,
                        "dividend_yield": 1.5,
                    },
                ],
            },
            lambda r: r["peer_averages"]["pe_ratio"] == pytest.approx(25.0)
            and r["peer_count"] == 2,
        ),
        "fundamentals.portfolio_returns": (
            {
                "transactions": [
                    {"transaction_type": "buy", "total_amount": 10000.0}
                ],
                "total_value": 11000.0,
                "daily_returns": [0.01, -0.005, 0.002],
                "period_days": 365,
            },
            lambda r: r["percentage_return"] == pytest.approx(10.0),
        ),
        "fundamentals.risk_metrics": (
            {"daily_returns": _FIXTURE_DAILY, "total_value": 10000.0},
            lambda r: r["data_quality"] == "historical"
            and r["data_points"] == 20
            and isinstance(r["value_at_risk_95"], float),
        ),
        "fundamentals.stress_tests": (
            {"total_value": 10000.0},
            lambda r: r["market_crash_20pct"] == pytest.approx(-2000.0),
        ),
        "fundamentals.correlation_matrix": (
            {
                "returns_by_symbol": {
                    "A": [0.01, 0.02, -0.01, 0.0, 0.03, -0.02, 0.015,
                          -0.005, 0.025, -0.015, 0.005, -0.025],
                    "B": [0.01, 0.02, -0.01, 0.0, 0.03, -0.02, 0.015,
                          -0.005, 0.025, -0.015, 0.005, -0.025],
                },
                "symbols": ["A", "B"],
            },
            lambda r: r["A"]["B"] == pytest.approx(1.0) and r["A"]["A"] == 1.0,
        ),
        "fundamentals.benchmark_comparison": (
            {"portfolio_return": 10.0, "benchmark_closes": [100.0, 120.0]},
            lambda r: r["sp500_return"] == pytest.approx(20.0)
            and r["alpha"] == pytest.approx(-10.0),
        ),
        "news.aggregate_market_sentiment": (
            {"rows": [("bullish", 70, 0.5), ("bearish", 30, -0.3)]},
            lambda r: r["overall"] == "bullish" and r["total_articles"] == 100,
        ),
        "news.score_portfolio_impact": (
            {
                "sentiment_score": 0.5,
                "sentiment": "bullish",
                "confidence": 0.8,
                "affected_tickers": ["AAPL", "MSFT"],
            },
            lambda r: r["impact_score"] == pytest.approx(60.0)
            and r["impact_type"] == "direct",
        ),
        "news.score_stocktwits_messages": (
            {
                "messages": [
                    {"entities": {"sentiment": {"basic": "Bullish"}}},
                    {"entities": {"sentiment": {"basic": "Bullish"}}},
                    {"entities": {"sentiment": {"basic": "Bearish"}}},
                ],
                "watchers": 1000,
            },
            lambda r: r["sentiment_score"] == pytest.approx(1 / 3)
            and r["bullish_count"] == 2,
        ),
        "news.stocktwits_sentiment": (
            {
                "symbol": "AAPL",
                "fetch_fn": _stocktwits_fetcher,
            },
            lambda r: r["sentiment_score"] == 1.0 and r["watchers"] == 5000,
        ),
    }


class TestRegistryShape:
    def test_every_bound_capability_is_invocable(self, registry):
        """The extracted registry is the opposite of the census: nothing here
        is aspirational."""
        assert len(registry) == registry.summary()["invocable"]
        assert registry.backlog() == []

    def test_claim_type_constant_matches_the_schema_enum(self):
        assert CLAIM_TYPES == SCHEMA_CLAIM_TYPES

    def test_every_declared_claim_type_is_in_the_closed_enum(self, registry):
        for name in registry._by_name:
            cap = registry.get(name)
            declared = set(cap.produces) | set(cap.consumes)
            assert declared <= SCHEMA_CLAIM_TYPES, (
                f"{name} declares a claim type outside the schema enum: {declared}"
            )

    def test_there_are_producers_for_the_shareable_query_test(self, registry):
        # A guard against an edit that removes every producer and makes the
        # shareable-exclusion test below vacuous.
        producers = {c.name for c in registry._by_name.values() if c.produces}
        assert producers == {
            "news.aggregate_market_sentiment",
            "news.score_stocktwits_messages",
            "news.stocktwits_sentiment",
        }


class TestInvocation:
    @pytest.mark.parametrize("name", list(_cases().keys()))
    async def test_every_capability_runs_and_produces_its_value(self, registry, name):
        kwargs, predicate = _cases()[name]
        result = await registry.get(name).call(**kwargs)
        assert predicate(result), f"{name} returned an unexpected result: {result!r}"

    async def test_a_missing_dependency_raises_rather_than_substituting(self, registry):
        """The fill pipeline needs the reason, not a silent default."""
        with pytest.raises(Unavailable):
            await registry.get("macro.sahm_rule").call(
                unemployment_values=[3.5, 3.6, 3.7]
            )

    async def test_empty_messages_raise_rather_than_neutral(self, registry):
        with pytest.raises(Unavailable):
            await registry.get("news.score_stocktwits_messages").call(
                messages=[], watchers=0
            )

    async def test_the_sync_and_async_wrappers_both_await_correctly(self, registry):
        # taylor_rule is sync; sahm_rule is async. Both must return through the
        # async `call` the binder builds.
        sync_out = await registry.get("macro.taylor_rule").call(
            inflation=2.0, output_gap=0.0
        )
        async_out = await registry.get("macro.sahm_rule").call(
            unemployment_values=[3.5] * 9 + [3.9, 4.1, 4.3]
        )
        assert sync_out == pytest.approx(2.5)
        assert "triggered" in async_out


class TestLicenceClassification:
    @pytest.mark.parametrize(
        "name",
        [
            "macro.sahm_rule",
            "macro.yield_curve_inversion",
            "macro.recession_probability",
            "macro.inflation_measures",
            "macro.pce_inflation",
            "macro.inflation_expectations",
            "macro.labor_market_tightness",
            "macro.taylor_rule",
            "macro.taylor_rule_variant",
            "macro.assess_policy_implications",
            "macro.assess_scenario_impact",
        ],
    )
    def test_macro_from_fred_is_shareable(self, registry, name):
        assert registry.get(name).touches_byo is False

    @pytest.mark.parametrize(
        "name",
        [
            "fundamentals.financial_ratios",
            "fundamentals.dcf_valuation",
            "fundamentals.peer_comparison",
            "fundamentals.stress_tests",
        ],
    )
    def test_edgar_sourced_fundamentals_are_shareable(self, registry, name):
        assert registry.get(name).touches_byo is False

    @pytest.mark.parametrize(
        "name",
        [
            "fundamentals.portfolio_returns",
            "fundamentals.risk_metrics",
            "fundamentals.correlation_matrix",
            "fundamentals.benchmark_comparison",
        ],
    )
    def test_portfolio_analytics_over_prices_inherit_the_price_licence(
        self, registry, name
    ):
        assert registry.get(name).touches_byo is True

    @pytest.mark.parametrize(
        "name",
        [
            "news.aggregate_market_sentiment",
            "news.score_portfolio_impact",
            "news.score_stocktwits_messages",
            "news.stocktwits_sentiment",
        ],
    )
    def test_news_and_sentiment_from_commercial_apis_are_private(
        self, registry, name
    ):
        assert registry.get(name).touches_byo is True

    def test_a_shareable_query_excludes_every_licensed_producer(self, registry):
        """How a planner avoids tainting an answer it intends to share."""
        assert registry.producing("perception_social", allow_byo=False) == []
        assert registry.producing("perception_news", allow_byo=False) == []
        assert len(registry.producing("perception_social")) == 2
        assert len(registry.producing("perception_news")) == 1

    def test_the_extracted_registry_has_no_shareable_producer_of_its_own(
        self, registry
    ):
        """The extracted caps transform already-ingested data; the shareable
        producers live in builtin (fred.series, edgar.companyfacts, ...). Here,
        asking for any shareable producer must come up empty."""
        for claim_type in SCHEMA_CLAIM_TYPES:
            assert registry.producing(claim_type, allow_byo=False) == []

    def test_entity_kinds_route_equities_away_from_macro(self, registry):
        assert registry.get("fundamentals.financial_ratios").entity_kinds == (
            "company",
        )
        assert registry.get("macro.inflation_measures").entity_kinds == ()


class TestCompositionWithBuiltin:
    def test_the_two_registries_share_no_capability_name(self, registry):
        builtin = build_builtin_registry()
        overlap = set(registry._by_name) & set(builtin._by_name)
        assert overlap == set(), f"name collision: {overlap}"

    def test_both_registries_combine_without_collision(self, registry):
        """The orchestrator mounts one registry; the two builders must compose."""
        builtin = build_builtin_registry()
        combined = Registry()
        for cap in registry._by_name.values():
            combined.add(cap)
        for cap in builtin._by_name.values():
            combined.add(cap)
        assert len(combined) == len(registry) + len(builtin)

    def test_builtin_prices_and_extracted_analytics_are_both_discoverable(
        self, registry
    ):
        builtin = build_builtin_registry()
        combined = Registry()
        for cap in registry._by_name.values():
            combined.add(cap)
        for cap in builtin._by_name.values():
            combined.add(cap)
        # The price producer (builtin) and the price consumer (extracted) coexist
        # without one shadowing the other.
        assert {c.name for c in combined.producing("price_snapshot")} == {
            "polygon.aggregates",
            "coingecko.market_chart",
        }
        assert combined.get("fundamentals.risk_metrics").consumes == (
            "price_snapshot",
        )
