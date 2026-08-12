"""The Discover universe is governed and omissions are explainable."""

from omni.market_universe import (
    CRYPTO_REGISTRY,
    MIN_CRYPTO_OBSERVATIONS,
    evaluate_crypto_census,
)


def _coin(rank: int, coin_id: str, symbol: str, name: str) -> dict:
    return {
        "market_cap_rank": rank,
        "id": coin_id,
        "symbol": symbol,
        "name": name,
    }


def test_census_classifies_registered_excluded_and_unmapped_assets() -> None:
    report = evaluate_crypto_census([
        _coin(1, "bitcoin", "btc", "Bitcoin"),
        _coin(3, "tether", "usdt", "Tether"),
        _coin(10, "new-major-asset", "new", "New Major Asset"),
        _coin(61, "outside-policy", "out", "Outside Policy"),
    ])

    assert [item["registered_symbol"] for item in report["included"]] == ["BTC"]
    assert report["excluded"][0]["reason"] == "stablecoin"
    assert report["unmapped"][0]["symbol"] == "NEW"
    assert all(item["symbol"] != "OUT" for item in report["unmapped"])


def test_registry_contains_previously_missed_large_assets() -> None:
    symbols = {metadata["symbol"] for metadata in CRYPTO_REGISTRY.values()}

    assert {"XMR", "TRX", "ZEC", "XLM", "HBAR", "SUI", "UNI", "AAVE"} <= symbols
    assert MIN_CRYPTO_OBSERVATIONS == 365
