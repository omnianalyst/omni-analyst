"""String and payee normalisation, ported from Actual Budget (MIT).

Source: packages/loot-core/src/shared/normalisation.ts and the
normalizeTransactions helpers in
packages/loot-core/src/server/accounts/sync.ts.
Copyright (c) James Long and Actual Budget contributors, MIT license.
"""

from __future__ import annotations

import re
import unicodedata

_CHAR_MAP = {
    "ł": "l",
    "ø": "o",
    "ß": "ss",
    "œ": "oe",
}
_CHAR_RE = re.compile("|".join(map(re.escape, _CHAR_MAP)))

_SMALL_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "in", "of",
    "on", "or", "the", "to", "vs", "via",
}


def normalised_string(value: str) -> str:
    replaced = _CHAR_RE.sub(lambda m: _CHAR_MAP[m.group(0)], value.lower())
    decomposed = unicodedata.normalize("NFD", replaced)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def title_case(value: str) -> str:
    """Actual's `title`: capitalise words, keep small words lowercase
    unless first or last, preserve existing capitals (McDonald stays)."""
    words = value.split()
    out = []
    for idx, word in enumerate(words):
        lower = word.lower()
        if (
            idx not in (0, len(words) - 1)
            and lower in _SMALL_WORDS
            and not word.isupper()
        ):
            out.append(lower)
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out)


def normalise_payee_name(name: str | None) -> str | None:
    if name is None:
        return None
    trimmed = name.strip()
    if trimmed == "":
        return None
    return title_case(trimmed)


def normalise_notes(notes: str | None) -> str | None:
    if notes is None:
        return None
    trimmed = notes.strip()
    if trimmed == "":
        return None
    return trimmed.replace("#", "##")


def amount_to_cents(value: str | float) -> int:
    """Actual stores integer minor units and rejects anything else; a
    float like 12.34 that is not exactly representable must round-trip
    through string parsing, never through float arithmetic."""
    if isinstance(value, int):
        return value
    text = str(value).strip().replace(",", "").replace("$", "")
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    if "." in text:
        whole, _, frac = text.partition(".")
        frac = (frac + "00")[:2]
    else:
        whole, frac = text, "00"
    cents = int(whole or "0") * 100 + int(frac or "0")
    return -cents if negative else cents


class ImportRowError(ValueError):
    pass


def normalise_import_row(row: dict) -> dict:
    """One incoming transaction, in Actual's normalised shape.

    Requires date and a payee of some form (payee_name or imported_payee);
    refuses rather than invents. Amount must convert to non-zero cents.
    """
    if row.get("date") in (None, ""):
        raise ImportRowError("`date` is required when adding a transaction")
    payee_name = normalise_payee_name(row.get("payee_name"))
    imported_payee = (row.get("imported_payee") or payee_name or "").strip() or None
    if payee_name is None and imported_payee is None:
        raise ImportRowError("`payee_name` is required when adding a transaction")
    raw_amount = row.get("amount")
    if raw_amount in (None, ""):
        raise ImportRowError("`amount` is required when adding a transaction")
    amount = amount_to_cents(raw_amount)
    if amount == 0:
        raise ImportRowError("amount must be non-zero")
    return {
        "date": str(row["date"])[:10],
        "amount": amount,
        "payee_name": payee_name,
        "imported_id": (row.get("imported_id") or None),
        "imported_payee": imported_payee,
        "notes": normalise_notes(row.get("notes")),
        "cleared": bool(row.get("cleared", True)),
        "category": row.get("category") or None,
        "raw": row.get("raw"),
    }
