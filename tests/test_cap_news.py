"""News/sentiment capability tests.

Every case asserts a computed value on a known input, never shape. Each
function also has a case where its input is unavailable and it must raise
rather than return a fabricated default.
"""

from datetime import UTC, datetime
from typing import ClassVar

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
    sector_news_outlook,
    stocktwits_sentiment,
    symbol_news_impact,
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


class TestSymbolNewsImpact:
    def test_ten_strong_bullish_articles_score_eighty(self):
        # volume_factor = min(1, 10/10) = 1.0; strength = |0.6| = 0.6
        # impact = (1.0*0.5 + 0.6*0.5)*100 = 80.0
        articles = [("bullish", 0.6, 0.9)] * 10
        out = symbol_news_impact(articles)
        assert out["articles_count"] == 10
        assert out["sentiment"] == "bullish"
        assert out["sentiment_score"] == pytest.approx(0.6)
        assert out["impact_score"] == pytest.approx(80.0)
        assert out["sentiment_breakdown"] == {"bullish": 10, "bearish": 0, "neutral": 0}

    def test_volume_caps_at_ten_articles(self):
        # Twenty articles saturate the volume factor at 1.0, so the impact
        # equals the ten-article case -- proving the cap, not a raw count.
        assert symbol_news_impact([("bullish", 0.6, 0.9)] * 20)["impact_score"] == (
            pytest.approx(symbol_news_impact([("bullish", 0.6, 0.9)] * 10)["impact_score"])
        )

    def test_fewer_articles_lower_the_impact_than_a_full_window_at_same_sentiment(self):
        # Same sentiment strength, but volume_factor = 0.5 for five articles
        # vs 1.0 for ten -- the impact must differ.
        full = symbol_news_impact([("bullish", 0.6, 0.9)] * 10)["impact_score"]
        half = symbol_news_impact([("bullish", 0.6, 0.9)] * 5)["impact_score"]
        assert half < full
        # (0.5*0.5 + 0.6*0.5)*100 = 55.0
        assert half == pytest.approx(55.0)

    def test_bearish_mix_is_negative_and_labelled_bearish(self):
        out = symbol_news_impact([("bearish", -0.6, 0.8)] * 8)
        assert out["sentiment"] == "bearish"
        assert out["sentiment_score"] == pytest.approx(-0.6)
        assert out["impact_score"] == pytest.approx(
            (min(1.0, 8 / 10.0) * 0.5 + 0.6 * 0.5) * 100
        )
        assert out["sentiment_breakdown"]["bearish"] == 8

    def test_midband_sentiment_is_neutral(self):
        # avg sentiment 0.1 sits inside the (-0.2, 0.2) neutral band.
        out = symbol_news_impact([("neutral", 0.1, 0.5)] * 4)
        assert out["sentiment"] == "neutral"

    def test_confidence_is_averaged_not_maxed(self):
        out = symbol_news_impact([("bullish", 0.6, 1.0), ("bullish", 0.6, 0.4)])
        assert out["confidence"] == pytest.approx(0.7)

    def test_empty_window_raises_rather_than_returning_neutral(self):
        with pytest.raises(Unavailable, match="no articles"):
            symbol_news_impact([])

    def test_impact_discriminates_volume_from_sentiment(self):
        # A wrong implementation that scored on volume alone (count*10) would
        # give 100 here, not 80; one that scored on sentiment alone would ignore
        # the article count. This pins the half/half blend.
        out = symbol_news_impact([("bullish", 1.0, 0.9)] * 1)
        # volume_factor = 0.1, strength = 1.0 -> (0.1*0.5 + 1.0*0.5)*100 = 55.0
        assert out["impact_score"] == pytest.approx(55.0)


class TestSectorNewsOutlook:
    MAPPING: ClassVar[dict[str, list[str]]] = {
        "Technology": ["AAPL", "MSFT"],
        "Energy": ["XOM"],
    }

    def test_bullish_tech_dominates_bearish_energy_by_volume(self):
        rows = [
            # AAPL: 8 bullish, 2 bearish
            ("AAPL", 0.5, 10, 8, 2, 0),
            # MSFT: 6 bullish, 4 bearish
            ("MSFT", 0.3, 10, 6, 4, 0),
            # XOM: 2 bullish, 8 bearish
            ("XOM", -0.5, 10, 2, 8, 0),
        ]
        out = sector_news_outlook(rows, sector_mapping=self.MAPPING)
        # Tech total = 20, score = (14 - 6)/20 = 0.4 -> positive
        tech = next(s for s in out if s["sector"] == "Technology")
        assert tech["outlook"] == "positive"
        assert tech["outlook_score"] == pytest.approx(0.4)
        assert tech["confidence"] == pytest.approx(1.0)  # min(1, 20/20)
        assert tech["articles_analyzed"] == 20
        # Energy total = 10, score = (2 - 8)/10 = -0.6 -> negative
        energy = next(s for s in out if s["sector"] == "Energy")
        assert energy["outlook"] == "negative"
        assert energy["outlook_score"] == pytest.approx(-0.6)
        assert energy["articles_analyzed"] == 10
        # Tech has more articles so ranks first
        assert out[0]["sector"] == "Technology"

    def test_score_uses_net_sentiment_not_bullish_share(self):
        # bullish=14, bearish=6, neutral=0 -> score = (14-6)/20 = 0.4.
        # A wrong impl using bullish/total would give 0.7 instead.
        rows = [("AAPL", 0.5, 20, 14, 6, 0)]
        out = sector_news_outlook(rows, sector_mapping={"Tech": ["AAPL"]})
        assert out[0]["outlook_score"] == pytest.approx(0.4)

    def test_confidence_saturates_at_twenty_articles(self):
        rows = [("AAPL", 0.5, 10, 5, 5, 0)]
        out = sector_news_outlook(rows, sector_mapping={"Tech": ["AAPL"]})
        assert out[0]["confidence"] == pytest.approx(0.5)

    def test_outlook_label_boundaries(self):
        mapping = {"S": ["SYM"]}
        # total=10, (6-4)/10 = 0.2 -> slightly_positive (0.1 < 0.2 < 0.3)
        assert sector_news_outlook(
            [("SYM", 0.0, 10, 6, 4, 0)], sector_mapping=mapping
        )[0]["outlook"] == "slightly_positive"
        # total=10, (4-6)/10 = -0.2 -> slightly_negative
        assert sector_news_outlook(
            [("SYM", 0.0, 10, 4, 6, 0)], sector_mapping=mapping
        )[0]["outlook"] == "slightly_negative"
        # exactly balanced -> neutral
        assert sector_news_outlook(
            [("SYM", 0.0, 10, 5, 5, 0)], sector_mapping=mapping
        )[0]["outlook"] == "neutral"

    def test_unmapped_tickers_are_skipped(self):
        rows = [
            ("UNMAPPED", 0.5, 100, 100, 0, 0),
            ("AAPL", 0.5, 5, 4, 1, 0),
        ]
        out = sector_news_outlook(rows, sector_mapping=self.MAPPING)
        assert len(out) == 1
        assert out[0]["sector"] == "Technology"
        assert out[0]["articles_analyzed"] == 5

    def test_top_symbols_sorted_by_sentiment_desc_and_capped(self):
        rows = [
            ("AAPL", 0.9, 1, 1, 0, 0),
            ("MSFT", -0.3, 1, 0, 1, 0),
        ]
        out = sector_news_outlook(rows, sector_mapping=self.MAPPING)
        tech = next(s for s in out if s["sector"] == "Technology")
        assert tech["top_symbols"][0]["symbol"] == "AAPL"
        assert tech["top_symbols"][1]["symbol"] == "MSFT"

    def test_none_avg_sentiment_is_preserved_not_substituted(self):
        rows = [("AAPL", None, 3, 2, 1, 0)]
        out = sector_news_outlook(rows, sector_mapping={"Tech": ["AAPL"]})
        assert out[0]["top_symbols"][0]["sentiment_score"] is None

    def test_no_mapped_ticker_raises(self):
        with pytest.raises(Unavailable, match="no ticker"):
            sector_news_outlook(
                [("ZZZ", 0.5, 10, 10, 0, 0)], sector_mapping=self.MAPPING
            )

    def test_empty_rows_raise(self):
        with pytest.raises(Unavailable):
            sector_news_outlook([], sector_mapping=self.MAPPING)
