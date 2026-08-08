"""v2's own capabilities — the set that actually runs."""

import pytest

from omni.capability.builtin import build_builtin_registry
from omni.ingest.protocol import ClaimDraft, Unavailable


class _NoCredentials:
    """Settings with nothing configured.

    Tests must not depend on whether this machine's .env happens to hold a
    key; a suite that passes or fails on ambient credentials is telling you
    about the machine, not the code.
    """

    fred_api_key = ""
    polygon_api_key = ""
    coingecko_api_key = ""
    etherscan_api_key = ""
    sec_user_agent = ""


@pytest.fixture
def registry():
    return build_builtin_registry(settings=_NoCredentials())


def test_every_builtin_is_actually_invocable(registry):
    """Unlike the census registry, nothing here is aspirational."""
    assert len(registry) == registry.summary()["invocable"]
    assert registry.backlog() == []


class TestLicenceClassification:
    @pytest.mark.parametrize(
        "name", ["fred.series", "fred.perception", "edgar.companyfacts",
                 "onchain.activity"],
    )
    def test_public_sources_are_shareable(self, registry, name):
        assert registry.get(name).touches_byo is False

    @pytest.mark.parametrize("name", ["polygon.aggregates", "coingecko.market_chart"])
    def test_licensed_price_feeds_are_private(self, registry, name):
        assert registry.get(name).touches_byo is True

    def test_no_price_source_is_redistributable(self, registry):
        """The licensing reality, made queryable rather than remembered.

        A planner building a shareable answer that needs prices must discover
        it cannot, instead of quietly using a licensed feed.
        """
        assert registry.producing("price_snapshot", allow_byo=False) == []

        # Previously `len(...) == 2`, which broke the moment the ccxt venues
        # were registered. A count was standing in for the property it could
        # not state: it is not that there are exactly two price sources, it is
        # that EVERY price source is licensed. Asserting the property directly
        # survives new venues and, unlike the count, actually fails if one of
        # them is ever misclassified as redistributable.
        producers = registry.producing("price_snapshot")
        assert producers, "no price producers registered at all"
        assert all(c.touches_byo for c in producers), [
            c.name for c in producers if not c.touches_byo
        ]

    def test_the_shareable_fundamental_layer_exists_for_both_asset_classes(
        self, registry
    ):
        """Equities have EDGAR; crypto has on-chain. That symmetry is the
        reason the coverage network has anything to accumulate."""
        assert registry.producing("fundamental_metric", allow_byo=False)
        assert registry.producing("onchain_tvl", allow_byo=False)


class TestSelection:
    def test_asking_for_a_claim_type_returns_only_its_producers(self, registry):
        names = {c.name for c in registry.producing("macro_series_point")}
        assert names == {"fred.series"}

    def test_a_capability_producing_several_types_is_found_by_each(self, registry):
        for t in ("onchain_flow", "onchain_tvl", "onchain_supply"):
            assert "onchain.activity" in {c.name for c in registry.producing(t)}

    def test_calibration_reorders_competing_producers(self, registry):
        # The exact list was pinned here before the ccxt venues existed. What
        # the test is actually for is that observed reliability changes the
        # order, so the baseline is now "polygon is not already first" -- which
        # still fails if the reordering is removed, and does not have to be
        # rewritten every time a venue is added.
        before = [c.name for c in registry.producing("price_snapshot")]
        assert "polygon.aggregates" in before
        assert "coingecko.market_chart" in before
        assert before[0] != "polygon.aggregates"

        registry.observe_reliability("polygon.aggregates", 0.9)
        assert registry.producing("price_snapshot")[0].name == "polygon.aggregates"


class TestInvocation:
    async def test_an_adapter_capability_runs_and_returns_drafts(self, registry):
        async def fake(series_id):
            return [{"date": "2024-01-01", "realtime_start": "2024-02-01",
                     "value": "1.5"}]

        drafts = await registry.get("fred.series").call("GDP", fetch_fn=fake)
        assert len(drafts) == 1
        assert isinstance(drafts[0], ClaimDraft)
        assert drafts[0].value == {"value": 1.5}

    async def test_an_unavailable_source_propagates(self, registry):
        """The fill pipeline needs the reason; it must not be swallowed here."""
        with pytest.raises(Unavailable):
            await registry.get("fred.series").call("GDP")

    async def test_the_detector_capability_runs_on_a_frame(self, registry):
        import numpy as np
        import pandas as pd

        n = 120
        idx = pd.date_range("2024-01-01", periods=n, freq="D")
        rng = np.random.default_rng(0)
        close = 100 + np.cumsum(rng.normal(0, 0.5, n))
        df = pd.DataFrame(
            {"open": close, "high": close + 1, "low": close - 1,
             "close": close, "volume": rng.integers(1e5, 2e5, n)},
            index=idx,
        )
        out = await registry.get("detect.manipulation").call(df)
        assert "findings" in out and "unsupported" in out

    async def test_the_detector_declares_what_it_cannot_do(self, registry):
        """Its provenance says so, and a planner should be able to read it."""
        cap = registry.get("detect.manipulation")
        assert "order-flow" in cap.provenance
        assert cap.consumes == ("price_snapshot",)


class TestSourceIsTheAdapterNotTheProvider:
    """`source` names the adapter that produced a row; `provider_key` names the
    vendor and the credential. They coincide for a single-venue adapter, which
    is why setting `source=provider_key` went unnoticed -- but `CCXTAdapter`
    serves five venues and stamps `ccxt`, and `DerivativesAdapter` stamps
    `derivatives`.

    The consequence is not cosmetic. `fill/pipeline.py` writes
    `source=(registration.source or registration.provider_key)`, and the
    idempotency index covers `source`, so a capability claiming `binance` would
    not collide with rows stored as `ccxt` -- the scheduler would write a second
    copy of the whole price spine, and `_price_window` (which does not filter on
    source) would take LIMIT window over a doubled series, silently halving the
    SMA window.
    """

    def test_every_capability_reports_the_source_its_adapter_stamps(self, registry):
        # The claim writer needs the adapter's label. Asserted for all of them,
        # not just the multi-venue ones, so a single-venue adapter that later
        # grows a second venue cannot quietly reintroduce the conflation.
        import importlib

        for name, capability in registry._by_name.items():
            if capability.provider_key is None:
                continue
            family = name.split(".")[0]
            try:
                module = importlib.import_module(f"omni.ingest.{family}")
            except ModuleNotFoundError:
                continue
            declared = getattr(module, "SOURCE", None)
            if declared is None:
                continue
            assert capability.source == declared, (
                f"{name} reports source={capability.source!r} but its adapter "
                f"stamps {declared!r}; the writer would not collide with rows "
                f"already stored under the adapter's label"
            )

    def test_venues_sharing_an_adapter_share_a_source(self, registry):
        # The specific defect: five ccxt venues had five different sources
        # because each took its own provider_key. Their provider_keys must stay
        # distinct -- that is the credential -- while the source is one value.
        families = ("exchanges", "microstructure")
        for family in families:
            caps = [c for n, c in registry._by_name.items() if n.startswith(f"{family}.")]
            assert len(caps) > 1, f"{family} should register several venues"
            assert len({c.source for c in caps}) == 1, (
                f"{family} capabilities disagree on source: "
                f"{sorted({c.source for c in caps})}"
            )
            assert len({c.provider_key for c in caps}) == len(caps), (
                f"{family} capabilities must keep distinct provider_keys"
            )

    def test_an_adapter_without_a_source_is_refused(self):
        from omni.capability.builtin import _source_of

        class Sourceless:
            pass

        with pytest.raises(ValueError, match="declares no `source`"):
            _source_of(Sourceless)
