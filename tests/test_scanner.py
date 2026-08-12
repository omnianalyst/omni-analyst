"""Measured rankings used by the Discover market scanner."""

from datetime import UTC, datetime, timedelta

from omni.api.scanner import SECTOR_RETURN_WINDOW, _sector_leader_payload


def _history(symbol: str, name: str, start: float, finish: float) -> list[dict]:
    origin = datetime(2026, 1, 1, tzinfo=UTC)
    step = (finish - start) / SECTOR_RETURN_WINDOW
    return [
        {
            "sector_symbol": "XLK",
            "sector_name": "Information Technology",
            "symbol": symbol,
            "name": name,
            "event_date": origin + timedelta(days=index),
            "value": {"close": start + step * index},
        }
        for index in range(SECTOR_RETURN_WINDOW + 1)
    ]


def test_sector_leaders_rank_returns_and_limit_the_display() -> None:
    rows = [
        *_history("FLAT", "Flat Corp", 100, 100),
        *_history("MID", "Middle Corp", 100, 110),
        *_history("TOP", "Top Corp", 100, 130),
        *_history("LOW", "Lower Corp", 100, 105),
    ]

    sectors = _sector_leader_payload(rows)

    assert len(sectors) == 1
    assert sectors[0]["coverage"] == 4
    assert [leader["symbol"] for leader in sectors[0]["leaders"]] == [
        "TOP",
        "MID",
        "LOW",
    ]
    assert sectors[0]["leaders"][0]["return_30d"] == 30.0


def test_sector_leaders_exclude_incomplete_or_invalid_histories() -> None:
    incomplete = _history("SHORT", "Short History", 100, 110)[:-1]
    invalid = _history("BAD", "Bad Data", 100, 110)
    for row in invalid:
        row["value"] = {"close": None}

    assert _sector_leader_payload([*incomplete, *invalid]) == []
