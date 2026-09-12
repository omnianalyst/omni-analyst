"""Parsers (OFX/QIF/CAMT/CSV detection) and bank-sync normalization,
ported from Actual's expectations (MIT)."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import httpx
import pytest

from omni.finance import banksync
from omni.finance.banksync import (
    BankSyncError,
    gocardless_transactions_to_rows,
    normalize_simplefin,
)
from omni.finance.parsers import detect_format, parse_any, parse_camt, parse_ofx, parse_qif

OFX_SGML = """OFXHEADER:100
DATA:OFXSGML

<OFX>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260115080000.000[-6:CST]<TRNAMT>-42.10<FITID>TX-001<NAME>KROGER<MEMO>groceries run</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260116<TRNAMT>1250.00<FITID>TX-002<NAME>EMPLOYER INC<MEMO>salary</STMTTRN>
</BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>"""

QIF = """!Type:Bank
D01/15/2026
T-42.10
PKroger
Mgroceries run
N1042
^
D01/16/2026
T1250.00
PEmployer Inc
^
"""

CAMT = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
  <BkToCstmrAcctRpt>
    <Rpt>
      <Ntry>
        <Amt Ccy="USD">42.10</Amt>
        <CdtDbtInd>DBIT</CdtDbtInd>
        <BookgDt><Dt>2026-01-15</Dt></BookgDt>
        <NtryRef>E-001</NtryRef>
        <RltdPties><Cdtr><Pty><Nm>Kroger</Nm></Pty></Cdtr></RltdPties>
        <RmtInf><Ustrd>groceries run</Ustrd></RmtInf>
      </Ntry>
      <Ntry>
        <Amt Ccy="USD">1250.00</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <BookgDt><Dt>2026-01-16</Dt></BookgDt>
        <NtryRef>E-002</NtryRef>
        <RltdPties><Cdtr><Pty><Nm>Employer Inc</Nm></Pty></Cdtr></RltdPties>
      </Ntry>
    </Rpt>
  </BkToCstmrAcctRpt>
</Document>"""


def test_detect_format_picks_the_right_parser():
    assert detect_format(OFX_SGML) == "ofx"
    assert detect_format(QIF) == "qif"
    assert detect_format(CAMT) == "camt"
    assert detect_format("Date,Amount\n2026-01-01,-1.00\n") == "csv"


def test_parse_ofx_extracts_dates_amounts_fitids():
    rows = parse_ofx(OFX_SGML)
    assert rows[0]["date"] == "2026-01-15"
    assert rows[0]["amount"] == "-42.10"
    assert rows[0]["payee_name"] == "KROGER"
    assert rows[0]["imported_id"] == "TX-001"
    assert rows[0]["notes"] == "groceries run"
    assert rows[1]["amount"] == "1250.00"


def test_parse_ofx_refuses_when_empty():
    with pytest.raises(ValueError):
        parse_ofx("<OFX></OFX>")


def test_parse_qif_maps_qif_letters():
    rows = parse_qif(QIF)
    assert rows[0]["date"] == "2026-01-15"
    assert rows[0]["amount"] == "-42.10"
    assert rows[0]["payee_name"] == "Kroger"
    assert rows[0]["imported_id"] == "check-1042"
    assert rows[1]["amount"] == "1250.00"


def test_parse_qif_rejects_bad_dates():
    with pytest.raises(ValueError):
        parse_qif("!Type:Bank\nD15 janvier\nT-1.00\n^\n")


def test_parse_camt_signs_amounts_by_direction():
    rows = parse_camt(CAMT)
    assert rows[0]["amount"] == "-42.10"
    assert rows[0]["date"] == "2026-01-15"
    assert rows[0]["payee_name"] == "Kroger"
    assert rows[0]["imported_id"] == "E-001"
    assert rows[1]["amount"] == "1250.00"


def test_parse_any_dispatches_by_format():
    assert len(parse_any(OFX_SGML)) == 2
    assert len(parse_any(QIF)) == 2
    assert len(parse_any(CAMT)) == 2


GOCARDLESS_BOOKED = [
    {
        "transactionId": "GC-1",
        "bookingDate": "2026-01-05",
        "transactionAmount": {"amount": "-42.10", "currency": "USD"},
        "creditorName": "KROGER",
        "remittanceInformationUnstructured": " groceries #1 ",
    }
]
GOCARDLESS_PENDING = [
    {
        "internalTransactionId": "INT-77",
        "valueDate": "2026-01-06",
        "transactionAmount": {"amount": "-10.00", "currency": "USD"},
        "debtorName": "COFFEE CO",
    }
]


def test_gocardless_normalization_follows_actual():
    rows = gocardless_transactions_to_rows(
        GOCARDLESS_BOOKED, GOCARDLESS_PENDING, "acct-ref"
    )
    booked = rows[0]
    assert booked["date"] == "2026-01-05"
    assert booked["amount"] == "-42.10"
    assert booked["payee_name"] == "KROGER"
    assert booked["cleared"] is True
    assert booked["imported_id"] == "GC-1"

    pending = rows[1]
    assert pending["date"] == "2026-01-06"
    assert pending["cleared"] is False
    # Actual's fallback: cleared-less rows without transactionId get
    # account-internalTransactionId as the imported id
    assert pending["imported_id"] == "acct-ref-INT-77"
    assert pending["payee_name"] == "COFFEE CO"


SIMPLEFIN_TXS = [
    {
        "id": "SF-1",
        "posted": 1767614400,
        "amount": "-42.10",
        "description": "Kroger",
        "memo": "card purchase",
    }
]


def test_simplefin_normalization():
    rows = normalize_simplefin(SIMPLEFIN_TXS)
    assert rows[0]["date"] == "2026-01-05"
    assert rows[0]["amount"] == "-42.10"
    assert rows[0]["payee_name"] == "Kroger"
    assert rows[0]["notes"] == "card purchase"
    assert rows[0]["imported_id"] == "SF-1"
    assert rows[0]["cleared"] is True


def test_simplefin_pending_rows_are_not_cleared():
    rows = normalize_simplefin(
        [dict(SIMPLEFIN_TXS[0], id="SF-2", pending=True)]
    )
    assert rows[0]["cleared"] is False


def test_simplefin_refuses_a_non_epoch_stamp():
    with pytest.raises(BankSyncError, match="posted epoch"):
        normalize_simplefin([dict(SIMPLEFIN_TXS[0], posted="2026-01-05")])
    with pytest.raises(BankSyncError, match="posted epoch"):
        normalize_simplefin([dict(SIMPLEFIN_TXS[0], posted=None)])


def test_simplefin_refuses_transactions_without_amount():
    with pytest.raises(BankSyncError):
        normalize_simplefin([{"id": "SF-x", "posted": "2026-01-05"}])


def _patch_http(monkeypatch, handler):
    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(banksync, "httpx", SimpleNamespace(AsyncClient=factory))


def _token_for(url: str) -> str:
    return base64.b64encode(url.encode()).decode()


ACCESS_URL = "https://smith:beacon@bridge.simplefin.org/v1"


async def test_simplefin_claim_exchanges_a_setup_token(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=ACCESS_URL)

    _patch_http(monkeypatch, handler)
    token = _token_for("https://bridge.simplefin.org/claim/abc123")
    assert await banksync.simplefin_claim(token) == ACCESS_URL
    assert seen == ["https://bridge.simplefin.org/claim/abc123"]


async def test_simplefin_claim_refuses_a_foreign_claim_host(monkeypatch):
    _patch_http(monkeypatch, lambda request: httpx.Response(200, text=ACCESS_URL))
    token = _token_for("https://evil.example/claim/abc123")
    with pytest.raises(BankSyncError, match="allowed claim URL"):
        await banksync.simplefin_claim(token)


async def test_simplefin_claim_refuses_a_non_base64_token():
    with pytest.raises(BankSyncError, match="not base64"):
        await banksync.simplefin_claim("not base64 !!")


async def test_simplefin_claim_reports_one_use_failure(monkeypatch):
    _patch_http(monkeypatch, lambda request: httpx.Response(404, text="gone"))
    token = _token_for("https://bridge.simplefin.org/claim/abc123")
    with pytest.raises(BankSyncError, match="obtain a new one"):
        await banksync.simplefin_claim(token)


async def test_simplefin_claim_refuses_a_bad_access_url(monkeypatch):
    _patch_http(
        monkeypatch,
        lambda request: httpx.Response(200, text="https://bridge.simplefin.org/no-creds"),
    )
    token = _token_for("https://bridge.simplefin.org/claim/abc123")
    with pytest.raises(BankSyncError, match="not a valid SimpleFIN credential"):
        await banksync.simplefin_claim(token)


async def test_simplefin_fetch_validates_the_stored_access_url():
    with pytest.raises(BankSyncError, match="not a valid SimpleFIN credential"):
        await banksync.simplefin_fetch_accounts("https://evil.example/x")


async def test_simplefin_fetch_keeps_provider_currency(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accounts": [{
            "id": "sf-1",
            "name": "Chequing",
            "currency": "CAD",
            "balance": "12.00",
            "transactions": [],
        }]})

    _patch_http(monkeypatch, handler)
    accounts = await banksync.simplefin_fetch_accounts(ACCESS_URL)
    assert accounts[0]["currency"] == "CAD"


def test_simplefin_currency_validator():
    assert banksync._simplefin_currency("cad") == "CAD"
    with pytest.raises(BankSyncError, match="unusable currency"):
        banksync._simplefin_currency("CA")
    with pytest.raises(BankSyncError, match="unusable currency"):
        banksync._simplefin_currency("C4D")
    with pytest.raises(BankSyncError, match="unusable currency"):
        banksync._simplefin_currency(None)


def test_gocardless_currency_validator():
    assert banksync._gocardless_currency(" EUR ") == "EUR"
    with pytest.raises(BankSyncError, match="unusable currency"):
        banksync._gocardless_currency("euros")
    with pytest.raises(BankSyncError, match="unusable currency"):
        banksync._gocardless_currency(None)
