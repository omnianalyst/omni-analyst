"""Tests for the ETF holdings ingestion parsers.

Every parser test feeds a recorded CSV snippet that matches the issuer's real
column layout and checks that the weights come out as the fractions an operator
would compute by hand. The snippets are small but structurally faithful:
metadata rows where the issuer has them, the real column names, and a footer
row that must not be counted as a holding.

The mutation check is implicit: swap the division by 100 for a pass-through, or
the header-scan for a hardcoded row index, and at least one assertion below
flips.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from omni.exposure.ingest import fetch_holdings, parse_ishares_csv, parse_vanguard_csv
from omni.ingest.protocol import Unavailable

_ISHARES_CSV = """\
"iShares iShares 20+ Year Treasury Bond ETF"
"As of date,2026-07-31"
"Total Net Assets,5000000000"
Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Shares,CUSIP,ISIN,SEDOL,Price,Location,Exchange,Currency,FX Rate,Maturity,Duration,YTM,Coupon,Mod Duration
AAPL,Apple Inc.,Information Technology,Equity,301000000,6.02,301000000,1500000,037833100,US0378331006,BMVYYN7,200.67,United States,NASDAQ,USD,1,---,---,---,---,---
MSFT,Microsoft Corp.,Information Technology,Equity,250000000,5.00,250000000,625000,594918104,US5949181045,--,400.00,United States,NASDAQ,USD,1,---,---,---,---,---
GOOGL,Alphabet Inc.,Communication Services,Equity,175000000,3.50,175000000,1166666,02079K305,US02079K305,--,150.00,United States,NASDAQ,USD,1,---,---,---,---,---
NVDA,NVIDIA Corp.,Information Technology,Equity,140000000,2.80,140000000,70000,123456789,US1234567890,--,2000.00,United States,NASDAQ,USD,1,---,---,---,---,---
Total,,,,-,17.32,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-
"""

_VANGUARD_CSV = """\
accresion,account,coupon,cusip,holdingsDate,issuer,issuerTicker,maturityDate,marketValue,quantity,valuationDate,holdingsPercent
0,VTI,---,037833100,2026-07-31,Apple Inc.,AAPL,---,301000000,1500000,2026-07-31,6.02
0,VTI,---,594918104,2026-07-31,Microsoft Corp.,MSFT,---,250000000,625000,2026-07-31,5.00
0,VTI,---,02079K305,2026-07-31,Alphabet Inc.,GOOGL,---,175000000,1166666,2026-07-31,3.50
0,VTI,---,123456789,2026-07-31,NVIDIA Corp.,NVDA,---,140000000,70000,2026-07-31,2.80
"""

_VANGUARD_ALT_WEIGHT_CSV = """\
accresion,cusip,holdingsDate,issuer,issuerTicker,marketValue,weight
0,037833100,2026-07-31,Apple Inc.,AAPL,301000000,6.02
0,594918104,2026-07-31,Microsoft Corp.,MSFT,250000000,5.00
"""


class TestIsharesParser:
    def test_extracts_weights_as_fractions(self):
        rows = parse_ishares_csv(_ISHARES_CSV)

        assert len(rows) == 4
        aapl = next(r for r in rows if r.ticker == "AAPL")
        assert aapl.weight == Decimal("0.0602")
        msft = next(r for r in rows if r.ticker == "MSFT")
        assert msft.weight == Decimal("0.0500")

    def test_strips_metadata_rows(self):
        rows = parse_ishares_csv(_ISHARES_CSV)
        tickers = {r.ticker for r in rows}
        assert "TOTAL" not in tickers
        assert tickers == {"AAPL", "MSFT", "GOOGL", "NVDA"}

    def test_preserves_cusip_and_name(self):
        rows = parse_ishares_csv(_ISHARES_CSV)
        aapl = next(r for r in rows if r.ticker == "AAPL")
        assert aapl.cusip == "037833100"
        assert aapl.name == "Apple Inc."

    def test_stops_at_total_footer(self):
        csv_with_extra = _ISHARES_CSV + (
            "EXTRA,Fake Holding,--,Equity,1000,0.01,1000,5,000000000,--,--,10.00,--,--,USD,1,---,---,---,---,---\n"
        )
        rows = parse_ishares_csv(csv_with_extra)
        assert "EXTRA" not in {r.ticker for r in rows}

    def test_raises_on_missing_header(self):
        with pytest.raises(Unavailable, match="no row containing"):
            parse_ishares_csv("just,some,random\ndata,here,now\n")

    def test_raises_on_header_but_no_data(self):
        header_only = (
            "Fund Name\n"
            "Ticker,Name,Weight (%)\n"
            "Total,,,0\n"
        )
        with pytest.raises(Unavailable, match="zero holdings"):
            parse_ishares_csv(header_only)

    def test_skips_rows_with_no_weight(self):
        csv_text = (
            "Ticker,Name,Weight (%)\n"
            "AAPL,Apple Inc.,6.02\n"
            "NODATA,No Weight Inc.,\n"
            "MSFT,Microsoft,5.00\n"
            "Total,,,11.02\n"
        )
        rows = parse_ishares_csv(csv_text)
        tickers = [r.ticker for r in rows]
        assert tickers == ["AAPL", "MSFT"]


class TestVanguardParser:
    def test_extracts_weights_from_holdingspercent(self):
        rows = parse_vanguard_csv(_VANGUARD_CSV)

        assert len(rows) == 4
        aapl = next(r for r in rows if r.ticker == "AAPL")
        assert aapl.weight == Decimal("0.0602")

    def test_handles_weight_column_name_variant(self):
        rows = parse_vanguard_csv(_VANGUARD_ALT_WEIGHT_CSV)

        assert len(rows) == 2
        assert rows[0].ticker == "AAPL"
        assert rows[0].weight == Decimal("0.0602")

    def test_raises_on_missing_weight_column(self):
        bad = "accresion,cusip,issuer\n0,12345,Apple Inc.\n"
        with pytest.raises(Unavailable, match="no row containing"):
            parse_vanguard_csv(bad)


class TestFetchHoldings:
    async def test_produces_claim_drafts(self):
        async def fake_fetch(url: str) -> str:
            assert "holdings.csv" in url
            return _ISHARES_CSV

        from datetime import UTC, datetime

        drafts = await fetch_holdings(
            "TLT",
            issuer="ishares",
            url="https://example.com/holdings.csv",
            fetch_fn=fake_fetch,
            as_of=datetime(2026, 7, 31, tzinfo=UTC),
        )

        assert len(drafts) == 4
        aapl = next(d for d in drafts if d.key == "AAPL")
        assert aapl.claim_type == "holding"
        assert aapl.value["weight"] == "0.0602"
        assert aapl.value["fund"] == "TLT"
        assert aapl.value["issuer"] == "ishares"
        assert aapl.confidence == 1.0
        assert aapl.event_date == datetime(2026, 7, 31, tzinfo=UTC)
        assert aapl.knowledge_date == datetime(2026, 7, 31, tzinfo=UTC)

    async def test_knowledge_date_defaults_to_as_of(self):
        async def fake_fetch(url: str) -> str:
            return _VANGUARD_CSV

        from datetime import UTC, datetime

        as_of = datetime(2026, 7, 31, tzinfo=UTC)
        drafts = await fetch_holdings(
            "VTI",
            issuer="vanguard",
            url="https://example.com/vti.csv",
            fetch_fn=fake_fetch,
            as_of=as_of,
        )
        for d in drafts:
            assert d.knowledge_date == as_of

    async def test_raises_on_unknown_issuer(self):
        from datetime import UTC, datetime

        async def fake_fetch(url: str) -> str:
            return ""

        with pytest.raises(Unavailable, match="no parser"):
            await fetch_holdings(
                "QQQ",
                issuer="invesco",
                url="https://example.com/qqq.csv",
                fetch_fn=fake_fetch,
                as_of=datetime(2026, 7, 31, tzinfo=UTC),
            )

    async def test_raises_on_fetch_failure(self):
        from datetime import UTC, datetime

        async def failing_fetch(url: str) -> str:
            raise ConnectionError("timeout")

        with pytest.raises(Unavailable, match="holdings fetch.*failed"):
            await fetch_holdings(
                "TLT",
                issuer="ishares",
                url="https://example.com/holdings.csv",
                fetch_fn=failing_fetch,
                as_of=datetime(2026, 7, 31, tzinfo=UTC),
            )

    async def test_raises_on_empty_response(self):
        from datetime import UTC, datetime

        async def empty_fetch(url: str) -> str:
            return ""

        with pytest.raises(Unavailable, match="empty response"):
            await fetch_holdings(
                "TLT",
                issuer="ishares",
                url="https://example.com/holdings.csv",
                fetch_fn=empty_fetch,
                as_of=datetime(2026, 7, 31, tzinfo=UTC),
            )

    async def test_knowledge_date_separate_from_event_date(self):
        async def fake_fetch(url: str) -> str:
            return _ISHARES_CSV

        from datetime import UTC, datetime

        drafts = await fetch_holdings(
            "TLT",
            issuer="ishares",
            url="https://example.com/holdings.csv",
            fetch_fn=fake_fetch,
            as_of=datetime(2026, 7, 31, tzinfo=UTC),
            knowledge_date=datetime(2026, 9, 1, tzinfo=UTC),
        )
        for d in drafts:
            assert d.event_date == datetime(2026, 7, 31, tzinfo=UTC)
            assert d.knowledge_date == datetime(2026, 9, 1, tzinfo=UTC)
