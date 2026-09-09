"""Parsers (OFX/QIF/CAMT/CSV detection) and bank-sync normalization,
ported from Actual's expectations (MIT)."""

from __future__ import annotations

import pytest

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
        "posted": "2026-01-05T12:00:00+00:00",
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


def test_simplefin_refuses_transactions_without_amount():
    with pytest.raises(BankSyncError):
        normalize_simplefin([{"id": "SF-x", "posted": "2026-01-05"}])
