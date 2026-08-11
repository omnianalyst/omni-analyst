from datetime import UTC, datetime

import pytest

from omni.polymarket.types import (
    Document,
    Estimation,
    MarketAtCutoff,
    MarketPricePoint,
    ResolvedMarket,
)


def _resolved(**overrides):
    base = {
        "condition_id": "0xabc",
        "question": "Will X happen?",
        "category": "Politics",
        "resolved_yes": True,
        "resolution_date": datetime(2024, 6, 1, tzinfo=UTC),
        "created_at": datetime(2024, 5, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return ResolvedMarket(**base)


class TestResolvedMarketValidation:
    def test_clean_inputs_construct(self):
        m = _resolved()
        assert m.condition_id == "0xabc"
        assert m.resolution_date.tzinfo is not None

    def test_empty_condition_id_refused(self):
        with pytest.raises(ValueError):
            _resolved(condition_id="   ")

    def test_empty_question_refused(self):
        with pytest.raises(ValueError):
            _resolved(question="")

    def test_naive_resolution_date_refused(self):
        with pytest.raises(ValueError, match="naive"):
            _resolved(resolution_date=datetime(2024, 6, 1))  # noqa: DTZ001

    def test_resolution_before_created_refused(self):
        with pytest.raises(ValueError, match="resolution_date .* precedes"):
            _resolved(
                created_at=datetime(2024, 6, 1, tzinfo=UTC),
                resolution_date=datetime(2024, 5, 1, tzinfo=UTC),
            )

    def test_negative_volume_refused(self):
        with pytest.raises(ValueError, match="volume"):
            _resolved(volume=-1.0)


class TestMarketPricePointValidation:
    def test_clean_point_constructs(self):
        p = MarketPricePoint(at=datetime(2024, 5, 15, tzinfo=UTC), yes_price=0.65)
        assert p.yes_price == 0.65

    def test_nan_price_refused(self):
        with pytest.raises(ValueError, match="finite"):
            MarketPricePoint(at=datetime(2024, 5, 15, tzinfo=UTC), yes_price=float("nan"))

    def test_out_of_range_price_refused(self):
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            MarketPricePoint(at=datetime(2024, 5, 15, tzinfo=UTC), yes_price=1.5)

    def test_naive_at_refused(self):
        with pytest.raises(ValueError, match="naive"):
            MarketPricePoint(at=datetime(2024, 5, 15), yes_price=0.5)  # noqa: DTZ001


class TestDocumentValidation:
    def test_clean_document_constructs(self):
        d = Document(at=datetime(2024, 5, 1, tzinfo=UTC), source="reuters", text="X")
        assert d.source == "reuters"

    def test_empty_text_refused(self):
        with pytest.raises(ValueError, match="text"):
            Document(at=datetime(2024, 5, 1, tzinfo=UTC), source="reuters", text="   ")


class TestMarketAtCutoff:
    def _market(self):
        return _resolved()

    def test_clean_cutoff_constructs(self):
        snap = MarketAtCutoff(
            market=self._market(),
            cutoff=datetime(2024, 5, 25, tzinfo=UTC),
            market_probability=0.6,
        )
        assert snap.market_probability == 0.6

    def test_cutoff_at_or_before_created_refused(self):
        with pytest.raises(ValueError, match="at or before created_at"):
            MarketAtCutoff(
                market=self._market(),
                cutoff=datetime(2024, 5, 1, tzinfo=UTC),
                market_probability=0.5,
            )

    def test_cutoff_at_or_after_resolution_refused(self):
        with pytest.raises(ValueError, match="at or after resolution_date"):
            MarketAtCutoff(
                market=self._market(),
                cutoff=datetime(2024, 6, 1, tzinfo=UTC),
                market_probability=0.5,
            )

    def test_post_cutoff_document_refused(self):
        with pytest.raises(ValueError, match="lookahead bias"):
            MarketAtCutoff(
                market=self._market(),
                cutoff=datetime(2024, 5, 25, tzinfo=UTC),
                market_probability=0.5,
                documents=(
                    Document(
                        at=datetime(2024, 5, 26, tzinfo=UTC),
                        source="future",
                        text="not yet knowable",
                    ),
                ),
            )


class TestEstimation:
    def test_clean_up(self):
        e = Estimation(
            chosen_bin=0.65,
            direction="up",
            confidence=0.65,
            raw_choice="0.65",
            market_id="m1",
            cutoff=datetime(2024, 5, 25, tzinfo=UTC),
        )
        assert e.direction == "up"

    def test_neutral_direction_refused(self):
        with pytest.raises(ValueError, match="direction"):
            Estimation(
                chosen_bin=0.5,
                direction="neutral",
                confidence=0.5,
                raw_choice="0.5",
                market_id="m1",
                cutoff=datetime(2024, 5, 25, tzinfo=UTC),
            )
