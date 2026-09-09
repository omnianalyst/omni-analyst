"""Statement parsers: OFX (SGML and XML), QIF, CAMT.053 XML, CSV.

Each returns the same raw row shape the CSV importer produces:
{"date", "payee_name", "amount", "notes", "imported_id"}. Parsers extract
what the file states and refuse what it does not -- no invented amounts,
no assumed dates.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from omni.finance.service import FinanceError, parse_csv


def detect_format(text: str) -> str:
    head = text.lstrip()[:512]
    if head.startswith(("OFX", "<OFX", "<?OFX")) or "<OFX>" in head:
        return "ofx"
    if head.startswith("!" + "Type:") or "!Type:" in head:
        return "qif"
    if "Document" in head and ("camt" in head.lower() or "BkToCstmr" in text):
        return "camt"
    return "csv"


def parse_any(text: str, fmt: str | None = None) -> list[dict]:
    format_ = (fmt or detect_format(text)).lower()
    if format_ == "ofx":
        return parse_ofx(text)
    if format_ == "qif":
        return parse_qif(text)
    if format_ == "camt":
        return parse_camt(text)
    if format_ == "csv":
        return parse_csv(text)
    raise FinanceError(f"unsupported import format {format_!r}")


def _sgml_tag(block: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>([^<\r\n>]*)", block, re.I)
    return m.group(1).strip() if m else None


def _ofx_date(value: str) -> str:
    digits = re.sub(r"[^0-9]", "", value)[:8]
    if len(digits) != 8:
        raise FinanceError(f"unrecognised OFX date {value!r}")
    return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"


def parse_ofx(text: str) -> list[dict]:
    blocks = re.findall(r"<STMTTRN>(.*?)</STMTTRN>", text, re.I | re.S)
    if not blocks:
        blocks = re.findall(r"<STMTTRN>(.*?)(?=<STMTTRN>|$)", text, re.I | re.S)
    rows = []
    for block in blocks:
        posted = _sgml_tag(block, "DTPOSTED")
        amount = _sgml_tag(block, "TRNAMT")
        if posted is None or amount is None:
            raise FinanceError("OFX transaction missing DTPOSTED or TRNAMT")
        payee = _sgml_tag(block, "NAME") or _sgml_tag(block, "PAYEE")
        rows.append({
            "date": _ofx_date(posted),
            "payee_name": payee,
            "amount": amount,
            "notes": _sgml_tag(block, "MEMO"),
            "imported_id": _sgml_tag(block, "FITID") or _sgml_tag(block, "CHECKNUM"),
        })
    if not rows:
        raise FinanceError("no transactions found in OFX statement")
    return rows


def _qif_date(value: str) -> str:
    from datetime import datetime

    cleaned = value.strip().strip("'").replace("'", "/")
    for pattern in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(cleaned.split("[")[0].strip(), pattern).date().isoformat()
        except ValueError:
            continue
    raise FinanceError(f"unrecognised QIF date {value!r}")


def parse_qif(text: str) -> list[dict]:
    rows = []
    current: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("!type:"):
            continue
        if line == "^":
            if current.get("date") and current.get("amount") is not None:
                rows.append(current)
            current = {}
            continue
        tag, value = line[:1], line[1:].strip()
        if tag == "D":
            current["date"] = _qif_date(value)
        elif tag in ("T", "U"):
            current["amount"] = value
        elif tag == "P":
            current["payee_name"] = value or None
        elif tag == "M":
            current["notes"] = value or None
        elif tag == "N":
            current.setdefault("imported_id", f"check-{value}" if value else None)
        elif tag == "C":
            current.setdefault("cleared", value.lower() in ("c", "r", "x"))
    if current.get("date") and current.get("amount") is not None:
        rows.append(current)
    if not rows:
        raise FinanceError("no transactions found in QIF file")
    return rows


def _camt_text(element, path: str, ns: dict) -> str | None:
    found = element.find(path, ns)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def parse_camt(text: str) -> list[dict]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise FinanceError(f"invalid CAMT.053 XML: {exc}") from exc
    ns = {"c": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    rows = []
    for entry in root.iter():
        if not entry.tag.endswith("Ntry"):
            continue
        date = None
        for date_el in entry.iter():
            if date_el.tag.endswith("}Dt") and date_el.text and len(date_el.text) == 10:
                date = date_el.text.strip()
                break
        amount_el = None
        for el in entry.iter():
            if el.tag.endswith("}Amt") and el.text:
                amount_el = el
                break
        sign = "DBIT"
        for el in entry.iter():
            if el.tag.endswith("}CdtDbtInd") and el.text:
                sign = el.text.strip()
                break
        if date is None or amount_el is None:
            raise FinanceError("CAMT entry missing date or amount")
        raw_amount = amount_el.text.strip()
        amount = raw_amount if sign != "DBIT" else f"-{raw_amount}"
        payee = None
        for el in entry.iter():
            if el.tag.endswith("}Nm") and el.text:
                payee = el.text.strip()
                break
        notes = None
        for el in entry.iter():
            if el.tag.endswith("}Ustrd") and el.text:
                notes = el.text.strip()
                break
        ref = None
        for el in entry.iter():
            if el.tag.endswith("}NtryRef") and el.text:
                ref = el.text.strip()
                break
            if el.tag.endswith("}AcctSvcrRef") and el.text and ref is None:
                ref = el.text.strip()
        rows.append({
            "date": date,
            "payee_name": payee,
            "amount": amount,
            "notes": notes,
            "imported_id": ref,
        })
    if not rows:
        raise FinanceError("no entries found in CAMT.053 report")
    return rows
