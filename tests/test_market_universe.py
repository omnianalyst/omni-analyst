"""The Discover universe is governed and omissions are explainable."""

import pytest

from omni.market_universe import (
    CRYPTO_REGISTRY,
    EXCLUDED_CRYPTO_IDS,
    MIN_CRYPTO_OBSERVATIONS,
    UNMAPPED_WITHOUT_A_MEASUREMENT,
    UNVERIFIED_CRYPTO_IDS,
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


def test_the_display_ladder_is_the_operators_seven_names() -> None:
    """One name per distinct user-facing role, per policy 2026-08-18.2.

    XMR stays deliberately: the privacy role, and the omission that once
    hid it is the documented lesson this census exists to prevent.
    """
    symbols = {metadata["symbol"] for metadata in CRYPTO_REGISTRY.values()}

    assert symbols == {"BTC", "ETH", "XRP", "SOL", "DOGE", "XMR", "HBAR"}
    assert MIN_CRYPTO_OBSERVATIONS == 365


def test_off_ladder_assets_are_excluded_not_silently_dropped() -> None:
    """Display removal is not coverage removal, and the census says so.

    Every id that left the registry carries the dated ladder reason so the
    coverage audit shows a deliberate policy where a silent disappearance
    would read as a gap.
    """
    off_ladder = [
        "tron", "hyperliquid", "leo-token", "zcash", "cardano", "whitebit",
        "bitcoin-cash", "the-open-network", "litecoin", "sui", "avalanche-2",
        "shiba-inu", "uniswap", "crypto-com-chain", "near", "okb",
        "bittensor", "htx-dao", "ondo-finance", "mantle", "aave",
        "polkadot", "internet-computer", "worldcoin-wld",
    ]
    for coin_id in off_ladder:
        assert coin_id in EXCLUDED_CRYPTO_IDS, coin_id
        assert "still ingested in the background" in EXCLUDED_CRYPTO_IDS[coin_id]
        assert coin_id not in CRYPTO_REGISTRY


def test_a_census_entry_off_the_ladder_reports_excluded_not_unmapped() -> None:
    report = evaluate_crypto_census([
        _coin(1, "bitcoin", "btc", "Bitcoin"),
        _coin(9, "cardano", "ada", "Cardano"),
    ])

    assert [item["registered_symbol"] for item in report["included"]] == ["BTC"]
    (item,) = report["excluded"]
    assert item["symbol"] == "ADA"
    assert "seven-name display ladder" in item["reason"]
    assert report["unmapped"] == []


class TestOperatorPolicyCuts:
    """The 2026-08-18 trim: BNB, LINK, XLM, ALGO left the governed universe.

    The rotation research had just closed with every ordering cell dead and
    per-tier alpha at zero, so the crypto section keeps one name per distinct
    user-facing role instead of breadth. The cut must stay visible in the
    census as a dated policy exclusion -- retiring it silently would turn a
    decision into a coverage gap, which is exactly what the unmapped list is
    reserved for.
    """

    def test_cut_symbols_are_absent_from_the_registry(self) -> None:
        symbols = {metadata["symbol"] for metadata in CRYPTO_REGISTRY.values()}
        assert not ({"BNB", "LINK", "XLM", "ALGO"} & symbols)

    def test_cut_ids_are_excluded_with_dated_operator_reasons(self) -> None:
        for coin_id in ("binancecoin", "chainlink", "stellar", "algorand"):
            assert coin_id in EXCLUDED_CRYPTO_IDS
            assert "operator policy 2026-08-18" in EXCLUDED_CRYPTO_IDS[coin_id]

    def test_the_census_reports_a_cut_as_excluded_not_unmapped(self) -> None:
        report = evaluate_crypto_census([
            _coin(1, "bitcoin", "btc", "Bitcoin"),
            _coin(8, "stellar", "xlm", "Stellar"),
        ])

        assert [item["registered_symbol"] for item in report["included"]] == ["BTC"]
        (item,) = report["excluded"]
        assert item["symbol"] == "XLM"
        assert "redundant with XRP" in item["reason"]
        assert report["unmapped"] == []


class TestUnmappedAssetsCarryWhatWasMeasured:
    """An unmapped asset is an open gap, and the gap keeps its finding.

    The seven live top-60 assets that cannot be mapped were re-audited on
    2026-08-13 and every obvious `<SYMBOL>-USD` ticker turned out to be a
    different coin. Recording that keeps the next audit from re-deriving it, and
    keeps the finding attached to the asset it is about rather than to a
    document nobody reads at the moment of the decision.
    """

    def test_a_measured_asset_reports_its_finding_instead_of_the_generic_reason(
        self,
    ) -> None:
        report = evaluate_crypto_census(
            [_coin(51, "memecore", "m", "MemeCore")]
        )

        (item,) = report["unmapped"]
        assert item["measured"] is True
        assert item["reason"] == UNVERIFIED_CRYPTO_IDS["memecore"]
        assert item["reason"] != UNMAPPED_WITHOUT_A_MEASUREMENT

    def test_an_unexamined_asset_is_marked_unmeasured_rather_than_explained(
        self,
    ) -> None:
        """The absence of a finding is itself the finding: nobody has looked."""
        report = evaluate_crypto_census(
            [_coin(10, "brand-new-asset", "bna", "Brand New Asset")]
        )

        (item,) = report["unmapped"]
        assert item["measured"] is False
        assert item["reason"] == UNMAPPED_WITHOUT_A_MEASUREMENT

    @pytest.mark.parametrize("coin_id", sorted(UNVERIFIED_CRYPTO_IDS))
    def test_a_measured_asset_is_still_reported_as_a_gap(self, coin_id: str) -> None:
        """The reason these are not in `EXCLUDED_CRYPTO_IDS`.

        Filing an asset that *should* rank alongside the stablecoins would
        retire the gap by renaming it, and it would leave `unmapped` -- the only
        place the coverage audit shows it. A measured obstacle is still an
        obstacle.
        """
        report = evaluate_crypto_census([_coin(20, coin_id, "sym", "Name")])

        assert len(report["unmapped"]) == 1
        assert report["excluded"] == []
        assert report["included"] == []

    def test_no_asset_is_both_excluded_by_policy_and_merely_unverified(self) -> None:
        """The two dicts answer different questions and must not overlap.

        `EXCLUDED_CRYPTO_IDS` is a judgement that survives any data; this one is
        a data problem that a verified symbol would end. An id in both would
        make the census's answer depend on which branch was checked first.
        """
        assert not (set(EXCLUDED_CRYPTO_IDS) & set(UNVERIFIED_CRYPTO_IDS))
        assert not (set(CRYPTO_REGISTRY) & set(UNVERIFIED_CRYPTO_IDS))

    def test_every_recorded_finding_states_what_it_measured(self) -> None:
        """A reason that does not carry its evidence is a reassurance.

        Each of these exists to stop the next audit re-running the comparison,
        which it can only do if it says what was compared and when.
        """
        for coin_id, reason in UNVERIFIED_CRYPTO_IDS.items():
            assert "-USD" in reason, coin_id
            assert "2026-" in reason, coin_id
            assert "CoinGecko" in reason, coin_id
