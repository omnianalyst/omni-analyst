"""News/sentiment capability tests.

Every case asserts a computed value on a known input, never shape. Each
function also has a case where its input is unavailable and it must raise
rather than return a fabricated default.
"""

from datetime import UTC, datetime

import pytest

from omni.capabilities.news import (
    aggregate_market_sentiment,
    calculate_signal_strength,
    calculate_trend,
    classify_sentiment,
    extract_ticker_entities,
    parse_feed_date,
    score_portfolio_impact,
    score_stocktwits_messages,
    stocktwits_sentiment,
)
from omni.ingest.protocol import Unavailable


class TestClassifySentiment:
    @pytest.mark.parametrize(
        "score, expected",
        [
            (0.5, "very_bullish"),
            (0.2, "bullish"),
            (0.05, "slightly_bullish"),
            (0.0, "neutral"),
            (-0.05, "neutral"),
            (-0.2, "slightly_bearish"),
            (-0.5, "bearish"),
            (-0.6, "very_bearish"),
        ],
    )
    def test_boundaries(self, score, expected):
        assert classify_sentiment(score) == expected

    def test_zero_is_neutral_not_slightly_bullish(self):
        assert classify_sentiment(0.0) == "neutral"


class TestCalculateTrend:
    def test_rising_series_is_improving(self):
        assert calculate_trend([0.0, 0.1, 0.2, 0.3]) == "improving"

    def test_falling_series_is_deteriorating(self):
        assert calculate_trend([0.3, 0.2, 0.1, 0.0]) == "deteriorating"

    def test_flat_series_is_stable(self):
        assert calculate_trend([0.5, 0.5, 0.5, 0.5]) == "stable"

    def test_slope_under_threshold_is_stable(self):
        assert calculate_trend([0.0, 0.005]) == "stable"

    def test_single_value_is_insufficient(self):
        assert calculate_trend([0.42]) == "insufficient_data"


class TestCalculateSignalStrength:
    def test_empty_is_no_signal(self):
        assert calculate_signal_strength([]) == "no_signal"

    def test_all_positive_is_strong_positive(self):
        assert calculate_signal_strength([("social", 0.5), ("news", 0.3)]) == "strong_positive"

    def test_all_negative_is_strong_negative(self):
        assert calculate_signal_strength([("social", -0.5), ("news", -0.3)]) == "strong_negative"

    def test_majority_positive_is_moderate_positive(self):
        assert (
            calculate_signal_strength([("a", 0.5), ("b", -0.5), ("c", 0.5)])
            == "moderate_positive"
        )

    def test_majority_negative_is_moderate_negative(self):
        assert (
            calculate_signal_strength([("a", -0.5), ("b", 0.5), ("c", -0.5)])
            == "moderate_negative"
        )

    def test_even_split_is_mixed(self):
        assert calculate_signal_strength([("a", 0.5), ("b", -0.5)]) == "mixed"

    def test_scores_inside_the_deadzone_do_not_count_as_directional(self):
        assert calculate_signal_strength([("a", 0.05), ("b", 0.05)]) == "mixed"


class TestParseFeedDate:
    def test_known_tuple(self):
        assert parse_feed_date((2024, 1, 15, 9, 30, 0)) == datetime(
            2024, 1, 15, 9, 30, 0, tzinfo=UTC
        )

    def test_only_first_six_elements_used(self):
        assert parse_feed_date((2024, 1, 15, 9, 30, 0, 0, 0, 0)) == datetime(
            2024, 1, 15, 9, 30, 0, tzinfo=UTC
        )

    def test_missing_date_raises_rather_than_inventing_now(self):
        with pytest.raises(Unavailable, match="no published date"):
            parse_feed_date(None)

    def test_empty_tuple_raises(self):
        with pytest.raises(Unavailable):
            parse_feed_date(())


class TestExtractTickerEntities:
    def test_extracts_real_tickers_and_drops_common_words(self):
        entities = extract_ticker_entities(
            title="AAPL and MSFT surge",
            summary="",
            content="CEO notes THE trend",
        )
        assert [e["entity_value"] for e in entities] == ["AAPL", "MSFT"]
        assert all(e["entity_type"] == "ticker" for e in entities)
        assert entities[0]["mentions_count"] == 1
        assert entities[1]["mentions_count"] == 1

    def test_repeat_mention_emits_one_entity_per_match_with_full_count(self):
        entities = extract_ticker_entities(
            title="AAPL AAPL", summary="", content=""
        )
        assert len(entities) == 2
        assert all(e["mentions_count"] == 2 for e in entities)

    def test_single_letter_tokens_are_excluded(self):
        entities = extract_ticker_entities(title="A B C", summary="", content="")
        assert entities == []

    def test_known_common_words_are_filtered(self):
        for word in ["THE", "CEO", "IPO", "ETF"]:
            entities = extract_ticker_entities(title=word, summary="", content="")
            assert entities == [], f"{word} should be filtered"


class TestAggregateMarketSentiment:
    def test_weighted_average_below_threshold_is_neutral(self):
        out = aggregate_market_sentiment(
            [("bullish", 60, 0.5), ("bearish", 40, -0.5)]
        )
        assert out["overall"] == "neutral"
        assert out["total_articles"] == 100
        assert out["breakdown"]["bullish"]["percentage"] == 60.0
        assert out["breakdown"]["bearish"]["percentage"] == 40.0
        assert out["breakdown"]["bullish"]["avg_score"] == 0.5
        assert out["breakdown"]["bearish"]["avg_score"] == -0.5

    def test_weighted_average_above_threshold_is_bullish(self):
        out = aggregate_market_sentiment(
            [("bullish", 80, 0.6), ("bearish", 20, -0.3)]
        )
        assert out["overall"] == "bullish"

    def test_none_avg_score_counts_as_zero_in_the_weighted_mean(self):
        out = aggregate_market_sentiment([("neutral", 5, None)])
        assert out["overall"] == "neutral"
        assert out["breakdown"]["neutral"]["avg_score"] == 0

    def test_empty_window_raises_rather_than_reporting_neutral(self):
        with pytest.raises(Unavailable, match="no sentiment data"):
            aggregate_market_sentiment([])

    def test_zero_total_raises_even_with_rows_present(self):
        with pytest.raises(Unavailable):
            aggregate_market_sentiment([("bullish", 0, None)])


class TestScorePortfolioImpact:
    def test_bullish_scales_by_affected_count(self):
        out = score_portfolio_impact(
            sentiment_score=0.5,
            sentiment="bullish",
            confidence=0.9,
            affected_tickers=["AAPL", "MSFT"],
        )
        assert out["impact_score"] == pytest.approx(60.0)
        assert out["impact_type"] == "direct"
        assert out["affected_holdings"] == ["AAPL", "MSFT"]
        assert out["analysis"]["affected_count"] == 2
        assert out["analysis"]["sentiment"] == "bullish"

    def test_bearish_is_negative_and_uses_the_same_scaling(self):
        out = score_portfolio_impact(
            sentiment_score=-0.5,
            sentiment="bearish",
            confidence=0.8,
            affected_tickers=["AAPL", "MSFT"],
        )
        assert out["impact_score"] == pytest.approx(-60.0)

    def test_neutral_sentiment_scores_zero_regardless_of_score(self):
        out = score_portfolio_impact(
            sentiment_score=0.9,
            sentiment="neutral",
            confidence=0.5,
            affected_tickers=["AAPL"],
        )
        assert out["impact_score"] == 0

    def test_no_affected_tickers_is_market_not_direct(self):
        out = score_portfolio_impact(
            sentiment_score=0.5,
            sentiment="bullish",
            confidence=0.9,
            affected_tickers=[],
        )
        assert out["impact_type"] == "market"
        assert out["impact_score"] == pytest.approx(50.0)


class TestScoreStocktwitsMessages:
    @staticmethod
    def _msg(basic):
        return {"entities": {"sentiment": {"basic": basic}}}

    def test_known_mix(self):
        out = score_stocktwits_messages(
            [self._msg("Bullish"), self._msg("Bullish"), self._msg("Bearish")],
            watchers=1500,
        )
        assert out["bullish_count"] == 2
        assert out["bearish_count"] == 1
        assert out["neutral_count"] == 0
        assert out["messages_analyzed"] == 3
        assert out["sentiment_score"] == pytest.approx(1 / 3)
        assert out["sentiment"] == "bullish"
        assert out["watchers"] == 1500

    def test_message_without_a_tag_counts_as_neutral(self):
        out = score_stocktwits_messages([{"entities": {}}], watchers=0)
        assert out["neutral_count"] == 1
        assert out["sentiment_score"] == 0.0
        assert out["sentiment"] == "neutral"

    def test_empty_messages_raise_rather_than_returning_neutral(self):
        with pytest.raises(Unavailable, match="no messages"):
            score_stocktwits_messages([], watchers=0)


class TestStocktwitsSentiment:
    async def test_injected_fetcher_needs_no_network(self):
        async def fake(sym):
            assert sym == "AAPL"
            return {
                "messages": [
                    {"entities": {"sentiment": {"basic": "Bullish"}}},
                    {"entities": {"sentiment": {"basic": "Bearish"}}},
                ],
                "symbol": {"watchlist_count": 4242},
            }

        out = await stocktwits_sentiment("AAPL", fetch_fn=fake)
        assert out["messages_analyzed"] == 2
        assert out["sentiment_score"] == 0.0
        assert out["watchers"] == 4242

    async def test_source_error_propagates(self):
        async def broken(sym):
            raise Unavailable("StockTwits returned HTTP 429")

        with pytest.raises(Unavailable, match="429"):
            await stocktwits_sentiment("AAPL", fetch_fn=broken)

    async def test_payload_with_no_messages_raises(self):
        async def empty(sym):
            return {"messages": [], "symbol": {"watchlist_count": 0}}

        with pytest.raises(Unavailable, match="no messages"):
            await stocktwits_sentiment("AAPL", fetch_fn=empty)


def test_a_feed_date_is_timezone_aware():
    """Feed timestamps are UTC. A naive datetime written to a TIMESTAMPTZ
    column is read as local time, so an article published at 14:00 UTC would
    land shifted by the process's offset — a silent error in a store whose
    premise is knowing when something became knowable."""
    assert parse_feed_date((2024, 1, 15, 9, 30, 0)).tzinfo is not None
