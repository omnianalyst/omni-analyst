"""ETF holdings ingestion: fetch issuer disclosures, write as holding claims.

Holdings data is public -- issuers publish it freely and file it with the SEC as
N-PORT -- so this source is ``allowed`` and accumulates into the shared network.
No credential, no audience scoping, no BYO path.

The adapter separates three concerns that are easy to entangle:

1. **Where to fetch.** Each issuer publishes holdings at a different URL with a
   different internal fund identifier, and those mappings change. The caller
   supplies ``fund_urls: {symbol: url}``; the adapter does not hardcode a URL
   it has not verified, because a fabricated URL is a silent 404 the fill loop
   records as ``unfillable`` and nothing distinguishes from a real outage.

2. **How to parse.** Each issuer's CSV has a different column layout. Per-issuer
   parsers (``parse_ishares_csv``, ``parse_vanguard_csv``) read the documented
   column names and produce ``(ticker, weight_fraction)`` pairs. A parser that
   guesses at column positions rather than reading named headers would break
   the first time the issuer reorders a column -- and CSV issuers reorder.

3. **How to write.** ClaimDrafts flow through ``write_claims``, which sets the
   licence class from the provider key. The adapter never decides who may see
   the data; it only declares where it came from.

**Weight is a fraction, not a percent.** iShares reports ``Weight (%)`` as
``6.02`` meaning 6.02%. The parser divides by 100 so the value stored in the
claim is ``0.0602``, matching the convention ``overlap.analyze`` multiplies
against ``allocation``.

**The holdings date is bitemporal.** ``event_date`` is the as-of date on the
holdings report (when the fund actually held this composition). ``knowledge_date``
is the publication date (when the filing became public). For monthly/quarterly
disclosures the gap is 30-60 days; conflating them lets a backtest see a
rebalance before the market did.
"""

from __future__ import annotations

import csv
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation

from omni.ingest.protocol import ClaimDraft, Unavailable

SOURCE = "etf_holdings"
PROVIDER_KEY = "etf_holdings"
CLAIM_TYPE = "holding"

__all__ = [
    "CLAIM_TYPE",
    "PROVIDER_KEY",
    "SOURCE",
    "HoldingRow",
    "fetch_holdings",
    "parse_ishares_csv",
    "parse_vanguard_csv",
]

Fetcher = Callable[[str], Awaitable[str]]


class HoldingRow:
    """One constituent parsed from an issuer CSV, before it becomes a claim.

    ``ticker`` may be ``"-"`` or blank for bonds that have no ticker (CUSIP
    only); the caller decides whether to use the CUSIP as the claim key or skip
    the row. Weight is a fraction (0.0 - 1.0).
    """

    __slots__ = ("cusip", "name", "ticker", "weight")

    def __init__(
        self,
        ticker: str,
        weight: Decimal,
        cusip: str | None = None,
        name: str | None = None,
    ) -> None:
        self.ticker = ticker
        self.weight = weight
        self.cusip = cusip
        self.name = name


def _clean_ticker(raw: str | None) -> str:
    if raw is None:
        return ""
    ticker = raw.strip()
    if ticker in ("-", "", "N/A", "NONE"):
        return ""
    return ticker.upper()


def _parse_weight(raw: str | None) -> Decimal | None:
    """Parse a weight percentage string (e.g. '6.02') to a fraction (0.0602).

    Returns None for absent or non-finite values rather than a fabricated zero.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        pct = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not pct.is_finite():
        return None
    return pct / Decimal(100)


def _find_header_row(
    lines: list[str], known_headers: set[str], max_scan: int = 30
) -> int | None:
    """Locate the column-header row in a CSV that has metadata above it.

    iShares prepends fund-level metadata (name, date, AUM) before the holdings
    table. The header row is the first one whose fields include the known column
    names. Scanning the first ``max_scan`` lines caps the search so a malformed
    file does not iterate to the end.
    """
    reader = csv.reader(lines)
    for i, row in enumerate(reader):
        if i >= max_scan:
            break
        normalised = {f.strip().lower() for f in row}
        if known_headers <= normalised:
            return i
    return None


def parse_ishares_csv(text: str) -> list[HoldingRow]:
    """Parse an iShares/BlackRock holdings CSV export.

    The iShares CSV format prepends several metadata rows (fund name, as-of
    date, total net assets, etc.), then a header row whose columns include
    ``Ticker`` and ``Weight (%)``, then data rows, then a ``Total`` footer.

    The parser locates the header by content rather than position so metadata
    row counts can vary between funds without breaking it.
    """
    lines = text.splitlines()
    header_idx = _find_header_row(lines, {"ticker", "weight (%)"})
    if header_idx is None:
        raise Unavailable(
            "iShares CSV has no row containing both 'Ticker' and 'Weight (%)' "
            "columns in the first 30 lines; the format may have changed"
        )

    reader = csv.DictReader(lines[header_idx:])
    rows: list[HoldingRow] = []
    for record in reader:
        ticker_field = record.get("Ticker")
        if ticker_field is None:
            break
        ticker_name = ticker_field.strip()
        if ticker_name.lower() in ("total", "total holdings", "cash", "cash & equivalents"):
            break

        ticker = _clean_ticker(ticker_name)
        weight = _parse_weight(record.get("Weight (%)"))
        if weight is None:
            continue
        cusip = (record.get("CUSIP") or "").strip() or None
        name = (record.get("Name") or "").strip() or None
        rows.append(HoldingRow(ticker=ticker, weight=weight, cusip=cusip, name=name))

    if not rows:
        raise Unavailable(
            "iShares CSV produced zero holdings rows after the header; either "
            "the fund is empty (which is wrong) or the column names drifted"
        )
    return rows


def parse_vanguard_csv(text: str) -> list[HoldingRow]:
    """Parse a Vanguard ETF holdings CSV.

    Vanguard's CSV starts directly with a header row (no metadata above it).
    The weight column has been seen as both ``holdingsPercent`` and ``weight``
    depending on fund type and download path; the parser locates either. The
    ticker column is ``ticker`` or ``issuerTicker``.
    """
    lines = text.splitlines()
    header_idx = _find_header_row(
        lines, {"holdingspercent"}, max_scan=15
    )
    if header_idx is None:
        header_idx = _find_header_row(lines, {"weight"}, max_scan=15)
    if header_idx is None:
        raise Unavailable(
            "Vanguard CSV has no row containing 'holdingsPercent' or 'weight' "
            "in the first 15 lines; the format may have changed"
        )

    reader = csv.DictReader(lines[header_idx:])
    weight_field = None
    ticker_field = None
    for raw_field in reader.fieldnames or []:
        low = raw_field.strip().lower()
        if low in ("holdingspercent", "weight") and weight_field is None:
            weight_field = raw_field
        if low in ("ticker", "issuerticker") and ticker_field is None:
            ticker_field = raw_field
    if weight_field is None or ticker_field is None:
        raise Unavailable(
            "Vanguard CSV header row lacks a weight or ticker column"
        )

    rows: list[HoldingRow] = []
    for record in reader:
        ticker = _clean_ticker(record.get(ticker_field))
        weight = _parse_weight(record.get(weight_field))
        if weight is None:
            continue
        cusip = (record.get("cusip") or record.get("CUSIP") or "").strip() or None
        name = (
            record.get("issuer")
            or record.get("description")
            or record.get("Name")
            or ""
        ).strip() or None

        if not ticker and not cusip:
            continue
        key_ticker = ticker or (cusip or "")
        rows.append(HoldingRow(ticker=key_ticker, weight=weight, cusip=cusip, name=name))

    if not rows:
        raise Unavailable(
            "Vanguard CSV produced zero holdings rows; the column names may "
            "have drifted or the fund has no reported holdings"
        )
    return rows


_ISSUER_PARSERS: dict[str, Callable[[str], list[HoldingRow]]] = {
    "ishares": parse_ishares_csv,
    "vanguard": parse_vanguard_csv,
}


async def fetch_holdings(
    symbol: str,
    *,
    issuer: str,
    url: str,
    fetch_fn: Fetcher,
    as_of: datetime,
    knowledge_date: datetime | None = None,
) -> list[ClaimDraft]:
    """Fetch and parse one ETF's holdings, producing claim drafts.

    ``issuer`` selects the parser (``"ishares"`` or ``"vanguard"``). ``url`` is
    the public holdings-download endpoint for this fund; the caller supplies it
    rather than the adapter hardcoding fund IDs it cannot verify.

    ``as_of`` is the holdings date printed on the report -- when the fund
    actually held this composition. ``knowledge_date`` defaults to ``as_of``
    when not given; for a live fetch of a dated report, the publication date
    can lag the holdings date by 30-60 days, and the caller should state it.
    """
    parser = _ISSUER_PARSERS.get(issuer)
    if parser is None:
        raise Unavailable(
            f"no parser for issuer {issuer!r}; supported: "
            f"{', '.join(sorted(_ISSUER_PARSERS))}"
        )
    if knowledge_date is None:
        knowledge_date = as_of

    try:
        text = await fetch_fn(url)
    except Exception as exc:
        raise Unavailable(f"holdings fetch for {symbol} from {url} failed: {exc}") from exc

    if not text or not text.strip():
        raise Unavailable(f"holdings fetch for {symbol} returned an empty response")

    rows = parser(text)
    drafts: list[ClaimDraft] = []
    for row in rows:
        if not row.ticker:
            continue
        drafts.append(
            ClaimDraft(
                claim_type=CLAIM_TYPE,
                key=row.ticker,
                value={
                    "weight": str(row.weight),
                    "fund": symbol,
                    "issuer": issuer,
                },
                event_date=as_of,
                knowledge_date=knowledge_date,
                confidence=1.0,
                evidence=(
                    {"cusip": row.cusip, "name": row.name}
                    if (row.cusip or row.name)
                    else None
                ),
            )
        )

    if not drafts:
        raise Unavailable(
            f"holdings for {symbol} parsed to zero usable rows with tickers"
        )
    return drafts
