"""Pre-trade risk: the layer whose job is to say no.

Every other module in the trading path is trying to find a reason to trade.
This one is the only place that is trying to find a reason not to, which makes
its default posture the inverse of everything around it: **absence of evidence
is a refusal, not a pass.** An unknown data age is stale. An unknown
correlation is a correlation. An unsupplied limit set is not "no limits", it is
"we do not know what is safe", and the answer to that is no.

That inversion is the whole design, because the failure this module exists to
stop is not a bad trade. It is a *bug* -- a mis-mapped side, a sizing function
that divided by a near-zero volatility, a state object that silently came back
empty after a database timeout -- turning into a position. Each of those
arrives looking like a perfectly ordinary intent. The only thing that
distinguishes them is that the numbers around them do not add up, and a checker
that fills in a missing number with a plausible default cannot see that.

So there are no defaults here that stand in for data. `correlations=None` does
not mean "uncorrelated", it means every existing position is assumed to move
with the intent, which is the assumption that keeps a portfolio from becoming
one bet wearing six tickers. `realised_pnl_today=None` does not mean "flat", it
means the daily loss limit cannot be evaluated and therefore has not been
cleared. A zero limit does not mean "unlimited", it means someone disabled a
safety check by typing a zero, and `RiskLimits` raises rather than accept it.

Refusals accumulate. The engine does not stop at the first one, because an
operator reading "position too large" and fixing the size, only to be told next
about the gross exposure, and then about the correlation, learns three times
slower than one reading all three at once -- and in the meantime is tempted to
widen the first limit they were shown.

No database, no venue, no clock beyond the one passed in. Pure functions over
values the caller has already gathered, so a risk decision can be reproduced
exactly from its inputs when it is questioned later.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Protocol, TypeGuard

from omni.venue.protocol import Position, Side, TradeIntent

ONE = Decimal(1)
ZERO = Decimal(0)


class RiskRefusal(str, Enum):
    POSITION_TOO_LARGE = "position_too_large"
    GROSS_EXPOSURE_EXCEEDED = "gross_exposure_exceeded"
    NET_EXPOSURE_EXCEEDED = "net_exposure_exceeded"
    TOO_MANY_POSITIONS = "too_many_positions"
    CORRELATION_LIMIT_EXCEEDED = "correlation_limit_exceeded"
    BELOW_MIN_NOTIONAL = "below_min_notional"
    ABOVE_MAX_NOTIONAL = "above_max_notional"
    DAILY_LOSS_LIMIT_HIT = "daily_loss_limit_hit"
    MAX_DRAWDOWN_HIT = "max_drawdown_hit"
    STALE_DATA = "stale_data"
    RECONCILIATION_DIVERGENCE = "reconciliation_divergence"
    TRADING_HALTED = "trading_halted"
    NO_LIMITS_CONFIGURED = "no_limits_configured"
    NO_STATE_AVAILABLE = "no_state_available"
    DAILY_PNL_UNKNOWN = "daily_pnl_unknown"
    PEAK_NAV_UNKNOWN = "peak_nav_unknown"
    RECONCILIATION_UNKNOWN = "reconciliation_unknown"


class PortfolioSnapshot(Protocol):
    """The read-only view this module needs; `portfolio/state.py` supplies it.

    Deliberately structural. Risk must be callable against a reconstructed
    historical snapshot in a backtest and against live state in the loop, and
    binding to a concrete class would make one of those a special case.
    """

    @property
    def nav(self) -> Decimal: ...

    @property
    def positions(self) -> Sequence[Position]: ...


def _usable(value: object) -> TypeGuard[Decimal]:
    """A number this module is willing to compute with.

    `Decimal` carries `NaN` and `Infinity` exactly as floats do, and every
    ordering comparison against a `Decimal` NaN raises `InvalidOperation`
    rather than returning False -- so an unchecked NaN NAV does not quietly
    pass a limit, it blows up inside the check with a message about decimal
    contexts. Screening here turns that into a named refusal instead.
    """
    return isinstance(value, Decimal) and value.is_finite()


@dataclass(frozen=True)
class RiskLimits:
    """The bounds a portfolio has agreed to operate inside.

    Every fraction is validated into `(0, 1]`. The upper bound is the reason
    gross exposure cannot be levered here: a limit set this module accepts is
    one where the portfolio cannot lose more than it has. The lower bound
    matters more -- a zero or negative limit reads as "unlimited" to any
    comparison written the obvious way, so a fat-fingered `0` would silently
    delete a safety check and every subsequent trade would pass it. Refusing at
    construction means the deletion is impossible rather than merely unlikely.
    """

    max_position_pct_nav: Decimal
    max_gross_exposure_pct_nav: Decimal
    max_net_exposure_pct_nav: Decimal
    max_positions: int
    max_correlated_exposure_pct_nav: Decimal
    correlation_threshold: Decimal
    min_notional: Decimal
    max_notional: Decimal
    daily_loss_limit_pct_nav: Decimal
    max_drawdown_pct: Decimal
    max_data_age: timedelta

    _FRACTIONS = (
        "max_position_pct_nav",
        "max_gross_exposure_pct_nav",
        "max_net_exposure_pct_nav",
        "max_correlated_exposure_pct_nav",
        "correlation_threshold",
        "daily_loss_limit_pct_nav",
        "max_drawdown_pct",
    )

    def __post_init__(self) -> None:
        for name in self._FRACTIONS:
            value = getattr(self, name)
            if not _usable(value):
                raise ValueError(f"{name} must be a finite Decimal, got {value!r}")
            if value <= ZERO:
                raise ValueError(
                    f"{name} must be positive, got {value}; a zero or negative "
                    f"limit is a disabled safety check, not an unlimited one"
                )
            if value > ONE:
                raise ValueError(
                    f"{name} is a fraction of NAV and must not exceed 1, got {value}"
                )

        for name in ("min_notional", "max_notional"):
            value = getattr(self, name)
            if not _usable(value):
                raise ValueError(f"{name} must be a finite Decimal, got {value!r}")
            if value <= ZERO:
                raise ValueError(f"{name} must be positive, got {value}")
        if self.max_notional <= self.min_notional:
            raise ValueError(
                f"max_notional {self.max_notional} must exceed min_notional "
                f"{self.min_notional}; an empty band admits no trade at all"
            )

        if self.max_positions < 1:
            raise ValueError(
                f"max_positions must be at least 1, got {self.max_positions}"
            )
        if self.max_data_age <= timedelta(0):
            raise ValueError(
                f"max_data_age must be positive, got {self.max_data_age}; "
                f"a non-positive age tolerance refuses every trade forever"
            )


@dataclass(frozen=True)
class RiskVerdict:
    allowed: bool
    refusals: tuple[RiskRefusal, ...]
    detail: str

    def __post_init__(self) -> None:
        if self.allowed and self.refusals:
            raise ValueError(f"an allowed verdict cannot carry refusals: {self.refusals}")
        if not self.allowed and not self.refusals:
            raise ValueError("a refusal must name its reason")

    def __bool__(self) -> bool:
        return self.allowed


def _correlation(
    a: str, b: str, correlations: Mapping[tuple[str, str], Decimal] | None
) -> Decimal | None:
    """The recorded correlation of two symbols, or None for "not known".

    None is the answer for an absent pair, an unusable number, and a value
    outside `[-1, 1]` alike. All three mean the same thing to the caller: this
    pair has not been measured, so it does not get to reduce the exposure the
    limit is protecting against.
    """
    if correlations is None:
        return None
    value = correlations.get((a, b))
    if value is None:
        value = correlations.get((b, a))
    if value is None or not _usable(value):
        return None
    if value < -ONE or value > ONE:
        return None
    return value


def check(
    intent: TradeIntent,
    state: PortfolioSnapshot | None,
    limits: RiskLimits | None,
    *,
    correlations: Mapping[tuple[str, str], Decimal] | None = None,
    data_as_of: datetime | None = None,
    now: datetime | None = None,
    realised_pnl_today: Decimal | None = None,
    peak_nav: Decimal | None = None,
    halted: bool = False,
    reconciled: bool | None = None,
) -> RiskVerdict:
    """Every reason this intent must not be sent, or an explicit permission.

    Exposure is evaluated on the portfolio *after* the intent, not on the
    intent alone. A sell into an existing long reduces the book, and a checker
    that scored it as fresh exposure would block the trades that de-risk while
    waving through the ones that concentrate -- which is the limit working
    backwards.

    Positions in symbols other than the intent's are marked at their average
    entry, because that is the only price this module has been given for them.
    The intent's own symbol is marked at `reference_price`, which is current
    and correct for the leg being decided.
    """
    refusals: list[RiskRefusal] = []
    detail: list[str] = []

    def refuse(reason: RiskRefusal, note: str) -> None:
        if reason not in refusals:
            refusals.append(reason)
        detail.append(note)

    if halted:
        refuse(RiskRefusal.TRADING_HALTED, "trading is halted")
    if reconciled is None:
        refuse(
            RiskRefusal.RECONCILIATION_UNKNOWN,
            "no reconciliation result was supplied; an unrun check has not "
            "been cleared",
        )
    elif not reconciled:
        refuse(
            RiskRefusal.RECONCILIATION_DIVERGENCE,
            "local state diverges from venue truth",
        )
    if limits is None:
        refuse(RiskRefusal.NO_LIMITS_CONFIGURED, "no risk limits supplied")

    if data_as_of is None:
        refuse(
            RiskRefusal.STALE_DATA,
            "data_as_of not supplied; an unknown age is not a fresh one",
        )
    elif limits is not None:
        reference = datetime.now(UTC) if now is None else now
        if (data_as_of.tzinfo is None) != (reference.tzinfo is None):
            refuse(
                RiskRefusal.STALE_DATA,
                f"cannot age data_as_of={data_as_of!r} against now={reference!r}: "
                f"one is timezone-aware and the other is naive",
            )
        else:
            age = reference - data_as_of
            if age < timedelta(0):
                refuse(
                    RiskRefusal.STALE_DATA,
                    f"data is stamped {-age} in the future; the clocks disagree "
                    f"and neither reading can be trusted",
                )
            elif age > limits.max_data_age:
                refuse(
                    RiskRefusal.STALE_DATA,
                    f"data age {age} exceeds {limits.max_data_age}",
                )

    nav = getattr(state, "nav", None) if state is not None else None
    raw_positions = getattr(state, "positions", None) if state is not None else None

    if state is None:
        refuse(RiskRefusal.NO_STATE_AVAILABLE, "no portfolio state supplied")
        return _verdict(refusals, detail)
    if not _usable(nav) or raw_positions is None:
        refuse(
            RiskRefusal.NO_STATE_AVAILABLE,
            f"portfolio state does not report a usable NAV and positions "
            f"(nav={nav!r})",
        )
        return _verdict(refusals, detail)

    if nav <= ZERO:
        refuse(
            RiskRefusal.NO_STATE_AVAILABLE,
            f"NAV is {nav}; a fraction-of-NAV limit expresses nothing against it",
        )
        return _verdict(refusals, detail)

    positions = list(raw_positions)
    unusable = [
        p
        for p in positions
        if not _usable(p.quantity) or not _usable(p.average_entry)
    ]
    if unusable:
        refuse(
            RiskRefusal.NO_STATE_AVAILABLE,
            f"{len(unusable)} position(s) carry a non-finite quantity or entry "
            f"price, starting with {unusable[0].symbol}",
        )
        return _verdict(refusals, detail)

    if limits is None:
        return _verdict(refusals, detail)

    notional = intent.notional
    if notional < limits.min_notional:
        refuse(
            RiskRefusal.BELOW_MIN_NOTIONAL,
            f"notional {notional} is below the {limits.min_notional} minimum",
        )
    if notional > limits.max_notional:
        refuse(
            RiskRefusal.ABOVE_MAX_NOTIONAL,
            f"notional {notional} exceeds the {limits.max_notional} maximum",
        )

    delta = intent.quantity if intent.side is Side.BUY else -intent.quantity
    same = [p for p in positions if p.symbol == intent.symbol]
    others = [p for p in positions if p.symbol != intent.symbol]

    resulting_quantity = sum((p.quantity for p in same), ZERO) + delta
    resulting_notional = abs(resulting_quantity) * intent.reference_price

    # Gross and net answer different questions and must not share a formula.
    #
    # Net is directional: a long spot leg against a short perp leg of the same
    # size is flat, and that is the whole point of a carry trade. Netting
    # across venues is correct here.
    #
    # Gross is capital deployed and counterparty exposure, and it does NOT net.
    # Those same two legs are two positions on two venues, each with its own
    # liquidation and its own custodian; a venue failing does not care that the
    # book was flat. Summing per row is correct here.
    #
    # The two were previously conflated: the intent's own symbol was netted
    # before being added to a gross built per row for every other symbol. A
    # cross-venue hedge in the traded name therefore reported zero gross while
    # the identical book in any other name reported it in full -- the limit
    # binding or not depending on which symbol happened to be traded.
    #
    # The intent is applied to its OWN venue's row rather than to the symbol as
    # a whole, so selling into a long on that venue reduces gross while selling
    # the same size on a different venue opens a second, opposing leg and
    # raises it. Applying the delta to the symbol would score those identically.
    resulting_book: dict[tuple[str, str], Decimal] = {}
    marks: dict[str, Decimal] = {}
    for position in positions:
        row = (position.venue, position.symbol)
        resulting_book[row] = resulting_book.get(row, ZERO) + position.quantity
        marks.setdefault(position.symbol, position.average_entry)
    intent_row = (intent.venue, intent.symbol)
    resulting_book[intent_row] = resulting_book.get(intent_row, ZERO) + delta
    marks[intent.symbol] = intent.reference_price

    gross = sum(
        (abs(quantity) * marks[symbol] for (_, symbol), quantity in resulting_book.items()),
        ZERO,
    )
    net = sum(
        (quantity * marks[symbol] for (_, symbol), quantity in resulting_book.items()),
        ZERO,
    )

    if resulting_notional > limits.max_position_pct_nav * nav:
        refuse(
            RiskRefusal.POSITION_TOO_LARGE,
            f"{intent.symbol} would be {resulting_notional} against a "
            f"{limits.max_position_pct_nav * nav} cap "
            f"({limits.max_position_pct_nav} of {nav} NAV)",
        )
    if gross > limits.max_gross_exposure_pct_nav * nav:
        refuse(
            RiskRefusal.GROSS_EXPOSURE_EXCEEDED,
            f"gross exposure would be {gross} against a "
            f"{limits.max_gross_exposure_pct_nav * nav} cap",
        )
    if abs(net) > limits.max_net_exposure_pct_nav * nav:
        refuse(
            RiskRefusal.NET_EXPOSURE_EXCEEDED,
            f"net exposure would be {net} against a "
            f"+/-{limits.max_net_exposure_pct_nav * nav} cap",
        )

    # Decimal equality against zero is exact -- the binary-float tolerance rule
    # does not apply -- and every non-finite quantity was refused above.
    by_symbol: dict[str, Decimal] = {}
    for position in positions:
        by_symbol[position.symbol] = (
            by_symbol.get(position.symbol, ZERO) + position.quantity
        )
    by_symbol[intent.symbol] = by_symbol.get(intent.symbol, ZERO) + delta
    open_count = sum(1 for quantity in by_symbol.values() if quantity != ZERO)
    if open_count > limits.max_positions:
        refuse(
            RiskRefusal.TOO_MANY_POSITIONS,
            f"{open_count} open positions would exceed the "
            f"{limits.max_positions} allowed",
        )

    correlated = resulting_notional
    unknown_pairs: list[str] = []
    for symbol in sorted({p.symbol for p in others}):
        symbol_notional = sum(
            (p.notional for p in others if p.symbol == symbol), ZERO
        )
        if symbol_notional <= ZERO:
            continue
        rho = _correlation(intent.symbol, symbol, correlations)
        if rho is None:
            unknown_pairs.append(symbol)
            correlated += symbol_notional
        elif abs(rho) > limits.correlation_threshold:
            # Magnitude, not sign: a short of a strongly anti-correlated name is
            # the same bet as a long of the intent, and summing absolute
            # notionals cannot tell the two apart. Treating -0.95 as
            # uncorrelated would let the limit be bypassed by inverting a leg.
            correlated += symbol_notional
    if correlated > limits.max_correlated_exposure_pct_nav * nav:
        unknown_note = (
            f"; correlation to {', '.join(unknown_pairs)} is unmeasured and was "
            f"assumed correlated"
            if unknown_pairs
            else ""
        )
        refuse(
            RiskRefusal.CORRELATION_LIMIT_EXCEEDED,
            f"correlated exposure would be {correlated} against a "
            f"{limits.max_correlated_exposure_pct_nav * nav} cap"
            f"{unknown_note}",
        )

    if realised_pnl_today is None:
        refuse(
            RiskRefusal.DAILY_PNL_UNKNOWN,
            "realised_pnl_today not supplied; the daily loss limit cannot be "
            "cleared and so has not been",
        )
    elif not _usable(realised_pnl_today):
        refuse(
            RiskRefusal.DAILY_PNL_UNKNOWN,
            f"realised_pnl_today is {realised_pnl_today}, which is not a number "
            f"a loss limit can be compared against",
        )
    elif realised_pnl_today < ZERO and -realised_pnl_today > (
        limits.daily_loss_limit_pct_nav * nav
    ):
        refuse(
            RiskRefusal.DAILY_LOSS_LIMIT_HIT,
            f"today's realised loss {-realised_pnl_today} exceeds the "
            f"{limits.daily_loss_limit_pct_nav * nav} daily limit",
        )

    if peak_nav is None:
        refuse(
            RiskRefusal.PEAK_NAV_UNKNOWN,
            "peak_nav not supplied; drawdown cannot be measured and the kill "
            "switch cannot be shown to be clear",
        )
    elif not _usable(peak_nav) or peak_nav <= ZERO:
        refuse(
            RiskRefusal.PEAK_NAV_UNKNOWN,
            f"peak_nav {peak_nav} is not a usable high-water mark",
        )
    else:
        drawdown = (peak_nav - nav) / peak_nav
        if drawdown > limits.max_drawdown_pct:
            refuse(
                RiskRefusal.MAX_DRAWDOWN_HIT,
                f"drawdown {drawdown} from peak NAV {peak_nav} exceeds "
                f"{limits.max_drawdown_pct}",
            )

    return _verdict(refusals, detail)


def _verdict(refusals: list[RiskRefusal], detail: list[str]) -> RiskVerdict:
    if refusals:
        return RiskVerdict(
            allowed=False,
            refusals=tuple(refusals),
            detail="; ".join(detail),
        )
    return RiskVerdict(
        allowed=True,
        refusals=(),
        detail="every configured limit was evaluated and cleared",
    )
