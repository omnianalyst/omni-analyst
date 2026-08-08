"""The bridge, tested for the inversion it exists to prevent.

Most of what this module does is arithmetic that other modules already own. The
one thing that is genuinely its own -- and the one thing no other layer can
check -- is that the prediction's barriers **swap** when the side is a sell. So
the weight here sits on three things:

- **The pair.** An `up` prediction and a `down` one built from the *same*
  barriers, asserted to produce opposite stop/target assignments. Either
  assertion alone passes for an implementation that ignores side entirely;
  only both together pin the swap.
- **The planted inversion.** `_barriers` is replaced with a version that does
  not swap and the bridge is driven through a short, proving the mapping error
  is caught at `TradeIntent` construction rather than discovered when the
  "stop" turns out to be a take profit. This is the regression that stops the
  swap from being quietly removed: delete it from `_barriers` and the pair
  fails; bypass `_barriers` and this one does.
- **The refusals, each next to the smallest variation that must be allowed.** A
  spot-only venue refuses a sell with no holding and permits the same sell
  against a position; a 0.5 hit rate against a symmetric payoff sizes to zero
  and a 0.6 hit rate against the same barriers does not. A bridge that refused
  everything would pass half of these and a bridge that refused nothing would
  pass the other half.

No database: every input is a plain value, which is the point of the signature.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from omni.portfolio.risk import RiskLimits, RiskRefusal, RiskVerdict
from omni.trading import bridge
from omni.trading.bridge import (
    BridgeRefusal,
    BridgeResult,
    _barriers,
    prediction_to_intent,
)
from omni.trading.policy import Eligibility, Ineligible, TradingPhase
from omni.venue.protocol import (
    Capabilities,
    InvalidIntent,
    MarketType,
    Position,
    Side,
    TradeIntent,
)

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
FRESH = NOW - timedelta(seconds=30)
HORIZON = NOW + timedelta(days=30)
PREDICTION_ID = UUID("11111111-2222-3333-4444-555555555555")

VENUE = "paper"
SYMBOL = "BTC/USD"
NAV = Decimal(100_000)

# entry 100 / upper 120 / lower 90.
#   long : b = 20/10 = 2, p = 0.6 -> f* = (0.6*2 - 0.4)/2 = 0.4
#   short: b = 10/20 = 0.5, p = 0.8 -> f* = (0.8*0.5 - 0.2)/0.5 = 0.4
# Quarter Kelly takes both to 0.1 of a 100k NAV at a price of 100 -> 100 units,
# a 10,000 notional that sits inside every limit below.
ENTRY = Decimal(100)
UPPER = Decimal(120)
LOWER = Decimal(90)
LONG_P = 0.6
SHORT_P = 0.8
EXPECTED_QUANTITY = Decimal(100)


@dataclass(frozen=True)
class FakeState:
    """The structural minimum the bridge and `risk.check` read."""

    nav: Decimal
    positions: tuple[Position, ...] = ()


def _prediction(
    direction: str = "up",
    *,
    entry: Decimal = ENTRY,
    upper: Decimal = UPPER,
    lower: Decimal = LOWER,
) -> dict:
    return {
        "id": PREDICTION_ID,
        "method": "trend.sma",
        "direction": direction,
        "confidence": 0.85,
        "entry_price": entry,
        "upper_barrier": upper,
        "lower_barrier": lower,
        "horizon_ends_at": HORIZON,
    }


def _eligibility(**overrides) -> Eligibility:
    base = {
        "eligible": True,
        "phase": TradingPhase.MICRO,
        "method": "trend.sma",
        "entity_kind": "crypto_asset",
        "resolved_n": 40,
        "live_resolved_n": 40,
        "hit_rate": LONG_P,
        "reason": None,
        "detail": "trend.sma/crypto_asset resolved correctly 60% of the time",
        "measured_n": 40,
    }
    return Eligibility(**{**base, **overrides})


def _limits(**overrides) -> RiskLimits:
    base = {
        "max_position_pct_nav": Decimal("0.20"),
        "max_gross_exposure_pct_nav": Decimal("0.50"),
        "max_net_exposure_pct_nav": Decimal("0.30"),
        "max_positions": 5,
        "max_correlated_exposure_pct_nav": Decimal("0.30"),
        "correlation_threshold": Decimal("0.60"),
        "min_notional": Decimal(100),
        "max_notional": Decimal(50_000),
        "daily_loss_limit_pct_nav": Decimal("0.02"),
        "max_drawdown_pct": Decimal("0.10"),
        "max_data_age": timedelta(minutes=5),
    }
    return RiskLimits(**{**base, **overrides})


def _capabilities(**overrides) -> Capabilities:
    base = {
        "spot": True,
        "margin": True,
        "perpetuals": False,
        "limit_orders": True,
        "shorting": True,
        "funding_data": False,
        "maker_fee_bps": Decimal(2),
        "taker_fee_bps": Decimal(10),
        "min_notional": Decimal(10),
    }
    return Capabilities(**{**base, **overrides})


def _position(quantity: Decimal, *, venue: str = VENUE, symbol: str = SYMBOL) -> Position:
    return Position(
        venue=venue,
        symbol=symbol,
        market_type=MarketType.SPOT,
        quantity=quantity,
        average_entry=ENTRY,
        as_of=NOW,
    )


async def _bridge(prediction: dict | None = None, **overrides) -> BridgeResult:
    call = {
        "eligibility": _eligibility(),
        "state": FakeState(nav=NAV),
        "limits": _limits(),
        "venue_name": VENUE,
        "capabilities": _capabilities(),
        "symbol": SYMBOL,
        "market_type": MarketType.SPOT,
        "data_as_of": FRESH,
        "now": NOW,
        "realised_pnl_today": Decimal(0),
        "peak_nav": NAV,
        "reconciled": True,
    }
    call.update(overrides)
    return await prediction_to_intent(
        _prediction() if prediction is None else prediction, **call
    )


class TestTheBarrierSwap:
    """The single most dangerous mapping in the module."""

    def test_a_buy_stops_at_the_lower_barrier_and_targets_the_upper(self):
        assert _barriers(Side.BUY, upper=UPPER, lower=LOWER) == (LOWER, UPPER)

    def test_a_sell_stops_at_the_upper_barrier_and_targets_the_lower(self):
        assert _barriers(Side.SELL, upper=UPPER, lower=LOWER) == (UPPER, LOWER)

    async def test_an_up_prediction_becomes_a_buy_stopping_at_the_lower_barrier(self):
        result = await _bridge(_prediction("up"))

        assert result.refusal is None
        assert result.intent is not None
        assert result.intent.side is Side.BUY
        assert result.intent.stop_price == LOWER
        assert result.intent.take_profit_price == UPPER
        assert result.intent.quantity == EXPECTED_QUANTITY
        assert bool(result) is True

    async def test_a_down_prediction_becomes_a_sell_stopping_at_the_upper_barrier(self):
        """The headline. Same barriers as the long, assigned the other way."""
        result = await _bridge(
            _prediction("down"), eligibility=_eligibility(hit_rate=SHORT_P)
        )

        assert result.refusal is None
        assert result.intent is not None
        assert result.intent.side is Side.SELL
        assert result.intent.stop_price == UPPER
        assert result.intent.take_profit_price == LOWER
        assert result.intent.stop_price > result.intent.reference_price
        assert result.intent.take_profit_price < result.intent.reference_price
        assert result.intent.quantity == EXPECTED_QUANTITY

    async def test_a_bridge_that_does_not_swap_the_barriers_raises(self, monkeypatch):
        """The regression that keeps the swap from being quietly removed.

        With `_barriers` returning (lower, upper) for every side, a short is
        built with a stop *below* its entry -- a take profit wearing a stop's
        name. Both orderings satisfy the schema's straddle constraint, so this
        is the only layer that can object, and it must.
        """
        monkeypatch.setattr(
            bridge, "_barriers", lambda side, *, upper, lower: (lower, upper)
        )

        with pytest.raises(InvalidIntent) as excinfo:
            await _bridge(
                _prediction("down"), eligibility=_eligibility(hit_rate=SHORT_P)
            )

        message = str(excinfo.value)
        assert "take_profit_price" in message
        assert "sell" in message


class TestTheFieldsCarriedStraightThrough:
    """Carried, not recomputed. Identity is asserted for exactly that reason.

    `Decimal(100) == Decimal("100.00")` and two equal datetimes compare equal,
    so an equality assertion would pass for an implementation that re-derived
    either value. Identity does not.
    """

    async def test_reference_price_is_the_prediction_entry_price(self):
        prediction = _prediction("up")
        result = await _bridge(prediction)

        assert result.intent.reference_price == ENTRY
        assert result.intent.reference_price is prediction["entry_price"]

    async def test_expires_at_is_the_prediction_horizon(self):
        prediction = _prediction("up")
        result = await _bridge(prediction)

        assert result.intent.expires_at == HORIZON
        assert result.intent.expires_at is prediction["horizon_ends_at"]

    async def test_the_venue_and_symbol_are_the_ones_asked_for(self):
        result = await _bridge(symbol="ETH/USD", venue_name="binance")

        assert result.intent.venue == "binance"
        assert result.intent.symbol == "ETH/USD"


class TestRefusals:
    async def test_a_neutral_prediction_produces_no_intent(self):
        result = await _bridge(_prediction("neutral"))

        assert result.refusal is BridgeRefusal.NEUTRAL_DIRECTION
        assert result.intent is None
        assert bool(result) is False

    async def test_an_ineligible_method_refuses_and_names_the_policy_reason(self):
        eligibility = _eligibility(
            eligible=False,
            reason=Ineligible.BELOW_HIT_RATE,
            detail="calibrated hit rate 0.40 is below the target 0.60 over 40 "
            "measured predictions",
        )

        result = await _bridge(eligibility=eligibility)

        assert result.refusal is BridgeRefusal.METHOD_INELIGIBLE
        assert result.intent is None
        assert Ineligible.BELOW_HIT_RATE.value in result.detail
        assert "0.40 is below the target 0.60" in result.detail
        assert result.eligibility is eligibility

    async def test_an_uncalibrated_hit_rate_refuses_without_reaching_sizing(
        self, monkeypatch
    ):
        """`sizing.size` raises on a None hit rate; this must refuse before it.

        The planted `size` proves the refusal is not merely the exception from
        two layers down wearing a different name, and that no default rate was
        substituted on the way past.
        """

        def _explode(**kwargs):
            raise AssertionError(
                "sizing was reached without a calibrated hit rate: "
                f"hit_rate={kwargs.get('hit_rate')!r}"
            )

        monkeypatch.setattr(bridge, "size", _explode)

        result = await _bridge(eligibility=_eligibility(hit_rate=None))

        assert result.refusal is BridgeRefusal.UNCALIBRATED_HIT_RATE
        assert result.intent is None

    async def test_a_symmetric_payoff_at_a_coin_flip_sizes_to_zero(self):
        flat = _prediction("up", upper=Decimal(110), lower=Decimal(90))

        result = await _bridge(flat, eligibility=_eligibility(hit_rate=0.5))

        assert result.refusal is BridgeRefusal.SIZED_TO_ZERO
        assert result.intent is None

    async def test_the_same_barriers_at_a_real_edge_do_not_size_to_zero(self):
        """The pair for the test above: only the hit rate differs."""
        flat = _prediction("up", upper=Decimal(110), lower=Decimal(90))

        result = await _bridge(flat, eligibility=_eligibility(hit_rate=0.6))

        assert result.refusal is None
        # f* = (0.6*1 - 0.4)/1 = 0.2; quarter Kelly 0.05 of 100k at 100 = 50.
        assert result.intent.quantity == Decimal(50)

    async def test_a_risk_refusal_carries_the_verdict_and_produces_no_intent(self):
        result = await _bridge(limits=_limits(max_notional=Decimal(5_000)))

        assert result.refusal is BridgeRefusal.RISK_REFUSED
        assert result.intent is None
        assert result.risk is not None
        assert result.risk.allowed is False
        assert RiskRefusal.ABOVE_MAX_NOTIONAL in result.risk.refusals
        assert "10000" in result.detail or "10000" in result.risk.detail

    async def test_a_sell_on_a_spot_only_venue_with_no_holding_refuses(self):
        result = await _bridge(
            _prediction("down"),
            eligibility=_eligibility(hit_rate=SHORT_P),
            capabilities=_capabilities(shorting=False, margin=False),
        )

        assert result.refusal is BridgeRefusal.VENUE_LACKS_CAPABILITY
        assert result.intent is None
        assert SYMBOL in result.detail

    async def test_the_same_sell_against_a_holding_is_a_reduction_and_is_allowed(self):
        """The pair: a spot venue can sell what it holds, it just cannot short."""
        result = await _bridge(
            _prediction("down"),
            eligibility=_eligibility(hit_rate=SHORT_P),
            capabilities=_capabilities(shorting=False, margin=False),
            state=FakeState(nav=NAV, positions=(_position(Decimal(100)),)),
        )

        assert result.refusal is None
        assert result.intent.side is Side.SELL
        assert result.intent.stop_price == UPPER

    async def test_a_holding_at_another_venue_does_not_permit_the_sell(self):
        """A long held elsewhere cannot be sold here; that sell opens a short."""
        result = await _bridge(
            _prediction("down"),
            eligibility=_eligibility(hit_rate=SHORT_P),
            capabilities=_capabilities(shorting=False, margin=False),
            state=FakeState(
                nav=NAV, positions=(_position(Decimal(100), venue="binance"),)
            ),
        )

        assert result.refusal is BridgeRefusal.VENUE_LACKS_CAPABILITY

    async def test_no_symbol_refuses(self):
        result = await _bridge(symbol=None)

        assert result.refusal is BridgeRefusal.NO_SYMBOL
        assert result.intent is None

    async def test_a_blank_symbol_is_no_symbol(self):
        result = await _bridge(symbol="   ")

        assert result.refusal is BridgeRefusal.NO_SYMBOL


class TestMissingInputsRaiseRatherThanDefault:
    """A size against an assumed NAV or an assumed cap is a fabricated size."""

    async def test_an_unknown_direction_raises(self):
        with pytest.raises(ValueError, match="unknown prediction direction"):
            await _bridge(_prediction("sideways"))

    async def test_no_portfolio_state_raises(self):
        with pytest.raises(ValueError, match="portfolio state is required"):
            await _bridge(state=None)

    async def test_no_limits_raise(self):
        with pytest.raises(ValueError, match="risk limits are required"):
            await _bridge(limits=None)

    async def test_a_neutral_prediction_still_refuses_without_state_or_limits(self):
        """The cheap refusals must not depend on inputs they never use."""
        result = await _bridge(_prediction("neutral"), state=None, limits=None)

        assert result.refusal is BridgeRefusal.NEUTRAL_DIRECTION


class TestBridgeResultCoherence:
    def _intent(self) -> TradeIntent:
        return TradeIntent(
            venue=VENUE,
            symbol=SYMBOL,
            side=Side.BUY,
            market_type=MarketType.SPOT,
            quantity=Decimal(1),
            reference_price=ENTRY,
        )

    def test_an_intent_and_a_refusal_together_raise(self):
        with pytest.raises(ValueError, match="cannot both hold"):
            BridgeResult(
                intent=self._intent(),
                refusal=BridgeRefusal.RISK_REFUSED,
                detail="both",
            )

    def test_neither_an_intent_nor_a_refusal_raises(self):
        with pytest.raises(ValueError, match="must name the refusal"):
            BridgeResult(intent=None, refusal=None, detail="neither")

    def test_an_intent_alone_is_truthy_and_a_refusal_alone_is_falsy(self):
        assert bool(BridgeResult(intent=self._intent(), refusal=None, detail="ok"))
        assert not bool(
            BridgeResult(
                intent=None, refusal=BridgeRefusal.NO_SYMBOL, detail="no symbol"
            )
        )

    def test_a_successful_result_carries_the_cleared_risk_verdict(self):
        verdict = RiskVerdict(allowed=True, refusals=(), detail="cleared")
        result = BridgeResult(
            intent=self._intent(), refusal=None, detail="ok", risk=verdict
        )

        assert result.risk is verdict
