"""SEC Form 4 insider-transaction research toolkit.

Standalone research module (no claim-store writes, no migrations) for testing
the insider-following event-study. Fetches Form 4 ownership XML from EDGAR,
parses non-derivative transactions into normalized records, and exposes the
builders the event-study needs.

PIT anchor is `filing_date` (the disclosure date), NEVER `transaction_date` --
insiders file within 2 business days, and joining on the trade date leaks 2
days of lookahead. filing_date is not in the XML body; it comes from the EFTS
search `_source.file_date` or the SGML header.

Side comes from `transactionAcquiredDisposedCode` (A=buy, D=sell), not the
transaction code -- M-code can be either direction.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

# Transaction codes that are open-market economic buys/sells (informed bets),
# per the event-study pre-registration. Grants (A), option exercises (M), tax
# withhold (F), gifts (G) are excluded -- compensation events, not signals.
BUY_CODES = {"P"}
SELL_CODES = {"S"}


@dataclass(frozen=True)
class InsiderTrade:
    filer_cik: str
    filer_name: str
    issuer_ticker: str
    issuer_cik: str
    transaction_date: str  # when the trade happened
    filing_date: str  # when it was disclosed -- the PIT anchor
    code: str  # transactionCode
    side: str  # "buy" | "sell" (from AcquiredDisposedCode A/D)
    shares: Decimal
    price: Decimal | None
    value: Decimal | None  # shares * price, None if price missing
    is_10b5_1: bool


def _txt(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    found = el.find(path)
    if found is None or found.text is None:
        return None
    return found.text.strip()


def parse_ownership_xml(
    xml_bytes: bytes, *, filing_date: str
) -> list[InsiderTrade]:
    """Parse one Form 4 ownership document into trade records.

    Only non-derivative, common-stock, open-market transactions (codes P/S)
    are emitted -- the pre-registered signal set. Derivative rows (options/RSUs)
    and compensation codes (A/M/F/G) are dropped here; the event-study netting
    uses only these.
    """
    root = ET.fromstring(xml_bytes)
    issuer_ticker = _txt(root, "issuer/issuerTradingSymbol") or ""
    issuer_cik = (_txt(root, "issuer/issuerCik") or "").lstrip("0")
    filer_cik = (_txt(root, "reportingOwner/reportingOwnerId/rptOwnerCik") or "").lstrip("0")
    filer_name = _txt(root, "reportingOwner/reportingOwnerId/rptOwnerName") or ""
    is_10b5 = _txt(root, "aff10b5One") == "1"

    out: list[InsiderTrade] = []
    for tx in root.findall(".//nonDerivativeTransaction"):
        title = _txt(tx, "securityTitle/value") or ""
        if "common stock" not in title.lower():
            continue
        code = _txt(tx, "transactionCoding/transactionCode") or ""
        if code not in BUY_CODES and code not in SELL_CODES:
            continue
        ad = _txt(tx, "transactionAmounts/transactionAcquiredDisposedCode/value")
        if ad == "A":
            side = "buy"
        elif ad == "D":
            side = "sell"
        else:
            continue

        shares_raw = _txt(tx, "transactionAmounts/transactionShares/value")
        price_raw = _txt(tx, "transactionAmounts/transactionPricePerShare/value")
        tdate = _txt(tx, "transactionDate/value") or filing_date
        if shares_raw is None:
            continue
        try:
            shares = Decimal(shares_raw)
            price = Decimal(price_raw) if price_raw is not None else None
        except Exception:
            continue
        value = shares * price if price is not None else None

        out.append(
            InsiderTrade(
                filer_cik=filer_cik,
                filer_name=filer_name,
                issuer_ticker=issuer_ticker,
                issuer_cik=issuer_cik,
                transaction_date=tdate,
                filing_date=filing_date,
                code=code,
                side=side,
                shares=shares,
                price=price,
                value=value,
                is_10b5_1=is_10b5,
            )
        )
    return out


def net_buy_value(trades: Sequence[InsiderTrade]) -> Decimal:
    """Dollar-weighted net insider buy = sum(buy value) - sum(sell value).

    The pre-registered entry signal. Trades with no price (NULL value) are
    dropped -- they are typically footnote-only grants, not marketable signals.
    """
    net = Decimal(0)
    for t in trades:
        if t.value is None:
            continue
        net += t.value if t.side == "buy" else (-t.value)
    return net
