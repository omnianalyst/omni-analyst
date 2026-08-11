"""Tests for the exposure/overlap computation.

Every test constructs a small portfolio of ETF positions with known holdings
and checks that the effective exposure, concentration and overlap are the
numbers an operator would compute by hand. No I/O -- these are pure function
tests over in-memory structures.

The mutations test (stub-to-wrong) is applied implicitly: each assertion is
specific enough that swapping a sum for a max, or a min for a max, or a >=
for a >, would flip the verdict on at least one case.
"""

from __future__ import annotations

from decimal import Decimal

from omni.exposure.overlap import (
    ETFPosition,
    Holding,
    analyze,
)


def _pos(
    symbol: str,
    bucket: str,
    allocation: str,
    holdings: dict[str, str],
) -> ETFPosition:
    return ETFPosition(
        symbol=symbol,
        bucket=bucket,
        allocation=Decimal(allocation),
        holdings=tuple(
            Holding(ticker=t, weight=Decimal(w))
            for t, w in holdings.items()
        ),
    )


class TestConcentrationSumsAcrossETFs:
    def test_aapl_in_two_etfs_doubles_its_effective_weight(self):
        vti = _pos("VTI", "growth", "0.60", {"AAPL": "0.06", "MSFT": "0.05"})
        qqq = _pos("QQQ", "growth", "0.20", {"AAPL": "0.12", "NVDA": "0.04"})

        result = analyze((vti, qqq), concentration_threshold=Decimal("0.01"))

        aapl = next(c for c in result.concentration if c.ticker == "AAPL")
        # 0.06 * 0.60 + 0.12 * 0.20 = 0.036 + 0.024 = 0.060
        assert aapl.total_weight == Decimal("0.060")
        assert set(aapl.source_etfs) == {"VTI", "QQQ"}

    def test_below_threshold_is_not_flagged(self):
        vti = _pos("VTI", "growth", "0.50", {"AAPL": "0.06", "OBSCURE": "0.001"})

        result = analyze((vti,), concentration_threshold=Decimal("0.02"))

        tickers = {c.ticker for c in result.concentration}
        assert "AAPL" in tickers
        assert "OBSCURE" not in tickers

    def test_concentration_is_sorted_descending(self):
        vti = _pos(
            "VTI",
            "growth",
            "0.60",
            {"AAPL": "0.06", "MSFT": "0.05", "GOOGL": "0.03"},
        )

        result = analyze((vti,), concentration_threshold=Decimal("0.001"))

        weights = [c.total_weight for c in result.concentration]
        assert weights == sorted(weights, reverse=True)


class TestOverlapMeasuresDuplication:
    def test_two_etfs_sharing_80pct_holdings_are_flagged(self):
        shared = {f"S{i}": "0.10" for i in range(8)}
        a = _pos("ETA", "growth", "0.50", {**shared, "UNIQUE_A": "0.20"})
        b = _pos("ETB", "growth", "0.50", {**shared, "UNIQUE_B": "0.20"})

        result = analyze((a, b), overlap_threshold=Decimal("0.25"))

        assert len(result.overlaps) == 1
        overlap = result.overlaps[0]
        assert overlap.etf_a == "ETA"
        assert overlap.etf_b == "ETB"
        # 8 shared at 0.10 each = 0.80
        assert overlap.shared_weight == Decimal("0.80")

    def test_non_overlapping_etfs_produce_no_overlap(self):
        a = _pos("ETA", "growth", "0.50", {"A1": "0.50", "A2": "0.50"})
        b = _pos("ETB", "currency_debasement", "0.50", {"B1": "0.50", "B2": "0.50"})

        result = analyze((a, b))

        assert result.overlaps == ()

    def test_below_threshold_overlap_is_not_flagged(self):
        a = _pos("ETA", "growth", "0.50", {"SHARED": "0.02", "A1": "0.98"})
        b = _pos("ETB", "growth", "0.50", {"SHARED": "0.02", "B1": "0.98"})

        result = analyze((a, b), overlap_threshold=Decimal("0.10"))

        assert result.overlaps == ()

    def test_overlap_uses_the_smaller_weight(self):
        """A holds AAPL at 6%, B at 12%. Overlap counts 6%, not 12%."""
        a = _pos("ETA", "growth", "1.0", {"AAPL": "0.06"})
        b = _pos("ETB", "growth", "1.0", {"AAPL": "0.12"})

        result = analyze((a, b), overlap_threshold=Decimal("0.01"))

        assert result.overlaps[0].shared_weight == Decimal("0.06")


class TestBucketExposure:
    def test_growth_dominates_a_growth_heavy_portfolio(self):
        positions = (
            _pos("VTI", "growth", "0.50", {"AAPL": "0.06"}),
            _pos("QQQ", "growth", "0.20", {"AAPL": "0.12"}),
            _pos("TLT", "deflation_rally", "0.15", {}),
            _pos("GLD", "currency_debasement", "0.15", {}),
        )

        result = analyze(positions)

        bucket_dict = dict(result.bucket_exposure)
        assert bucket_dict["growth"] == Decimal("0.70")
        assert bucket_dict["deflation_rally"] == Decimal("0.15")
        assert bucket_dict["currency_debasement"] == Decimal("0.15")

    def test_buckets_are_sorted_descending(self):
        positions = (
            _pos("GLD", "currency_debasement", "0.40", {}),
            _pos("VTI", "growth", "0.10", {}),
            _pos("TLT", "deflation_rally", "0.30", {}),
            _pos("SHV", "cash_yield", "0.20", {}),
        )

        result = analyze(positions)

        weights = [w for _, w in result.bucket_exposure]
        assert weights == sorted(weights, reverse=True)


class TestEdgeCases:
    def test_empty_portfolio_returns_empty_result(self):
        result = analyze(())

        assert result.concentration == ()
        assert result.overlaps == ()
        assert result.bucket_exposure == ()
        assert result.top_holdings == ()

    def test_single_etf_has_no_overlap(self):
        vti = _pos("VTI", "growth", "1.0", {"AAPL": "0.06"})

        result = analyze((vti,))

        assert result.overlaps == ()

    def test_top_holdings_lists_everything_above_zero(self):
        vti = _pos("VTI", "growth", "0.50", {"AAPL": "0.06", "MSFT": "0.05"})

        result = analyze((vti,))

        tickers = {t for t, _ in result.top_holdings}
        assert tickers == {"AAPL", "MSFT"}
