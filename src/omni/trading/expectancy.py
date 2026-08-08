"""Realised expectancy, and the three things that make a good number untrue.

`policy.py` barred on hit rate. Hit rate is only a proxy for edge when payoffs
are symmetric, and `trend.sma`'s are not: its stop IS the moving average, close
to price, while its target is a volatility-scaled move further out. Losing more
often than winning is the designed behaviour of a trend follower.

Measured on a year of real coverage, that method resolved 424 crypto
predictions at a 34.2% hit rate -- an interval entirely below a coin flip -- and
earned **+29.2 bps per trade** on a 4.32:1 payoff. A 34% strategy at 4.3:1 is a
good trade; the same rate at 1:1 is ruinous. The gate could not tell them apart,
so it refused the profitable one.

This module computes the quantity the gate should have been reading. It also
reports three properties of the sample, because each one can make a healthy
expectancy meaningless, and all three were present in that very first run:

**Concentration.** Pooled +29.2 bps was carried by two of nine assets (ADA
+114, DOT +98) against one deeply negative (UNI -149). A strategy that works on
two names is a position, not an edge, and the pooled mean hides that completely.

**Assumed P&L.** 35% of those predictions expired without touching a barrier.
The ledger records that they expired; it does not record the price they expired
at, so their P&L is *assumed* to be zero. Over a third of the sample therefore
contributes a number nobody measured. That is not conservative or optimistic --
it is unknown, and it must be visible rather than averaged in silently.

**Effective sample.** 424 predictions spanned only 44 distinct horizon dates:
nine highly-correlated crypto assets resolving on the same day are close to one
observation, not nine. A confidence interval computed on n=424 is far too tight.
`effective_n` reports the count of distinct horizons instead, which is the
conservative reading and roughly a tenth of the raw figure.

None of these is a reason to refuse by itself. All three are reasons a caller
must see the number's shape and not just its sign.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

BPS = Decimal(10_000)


@dataclass(frozen=True)
class ResolvedTrade:
    """One resolved prediction, reduced to what expectancy needs."""

    entity_key: str
    direction: str
    outcome: str
    entry_price: Decimal
    upper_barrier: Decimal
    lower_barrier: Decimal
    horizon_key: str

    def __post_init__(self) -> None:
        if self.entry_price <= 0:
            raise ValueError(f"entry_price must be positive: {self.entry_price}")
        if not (self.lower_barrier < self.entry_price < self.upper_barrier):
            raise ValueError(
                f"barriers must straddle entry: {self.lower_barrier} < "
                f"{self.entry_price} < {self.upper_barrier}"
            )
        if self.outcome == "pending":
            raise ValueError("a pending prediction has no realised P&L")

    @property
    def target_bps(self) -> Decimal:
        return (self.upper_barrier - self.entry_price) / self.entry_price * BPS

    @property
    def stop_bps(self) -> Decimal:
        return (self.entry_price - self.lower_barrier) / self.entry_price * BPS

    @property
    def is_assumed(self) -> bool:
        """True when the P&L is assumed rather than measured.

        An expiry means neither barrier was touched, so the position closed at
        whatever the horizon price was -- and the ledger does not store it.
        Scoring it as zero is the only option available, and the only honest
        thing to do with that is count how often it happens.
        """
        return self.outcome == "expiry"

    @property
    def pnl_bps(self) -> Decimal:
        """Realised P&L in basis points of entry.

        A long that hit the upper barrier earns the target; a long that hit the
        lower barrier pays the stop. A short is the mirror. Sign is taken from
        the pairing of direction and outcome, never from the outcome alone --
        `lower` is a win for a short and a loss for a long, and treating the
        barrier as the sign is how a P&L gets inverted for half the book.
        """
        if self.is_assumed:
            return Decimal(0)
        if self.direction == "up":
            return self.target_bps if self.outcome == "upper" else -self.stop_bps
        if self.direction == "down":
            return self.stop_bps if self.outcome == "lower" else -self.target_bps
        # A neutral call asserts no direction, so neither barrier is a win.
        return Decimal(0)


@dataclass(frozen=True)
class Expectancy:
    """Realised expectancy and the shape of the sample it came from."""

    n: int
    gross_bps: Decimal
    assumed_n: int
    distinct_horizons: int
    per_entity: tuple[tuple[str, int, Decimal], ...]

    @property
    def assumed_share(self) -> Decimal:
        if self.n == 0:
            return Decimal(0)
        return Decimal(self.assumed_n) / Decimal(self.n)

    @property
    def effective_n(self) -> int:
        """The conservative sample size.

        Predictions resolving on one horizon date across correlated assets are
        not independent observations. Rather than estimate a correlation this
        module has no data to estimate, it takes the number of distinct horizon
        dates -- which assumes assets are perfectly correlated. That is
        deliberately the pessimistic end: the truth lies between
        `distinct_horizons` and `n`, and a gate should stand on the near side.
        """
        return min(self.n, self.distinct_horizons)

    @property
    def positive_entities(self) -> int:
        return sum(1 for _, _, mean in self.per_entity if mean > 0)

    @property
    def concentration(self) -> Decimal:
        """Share of total P&L contributed by the single best entity.

        1.0 means one name is the whole result. Near 1/len(per_entity) means it
        is spread evenly. Computed on absolute contribution so a large loser
        counts toward concentration too -- a book carried by one winner and one
        loser is not diversified just because they cancel.
        """
        if not self.per_entity:
            return Decimal(0)
        totals = [abs(mean * Decimal(count)) for _, count, mean in self.per_entity]
        grand = sum(totals, Decimal(0))
        if grand == 0:
            return Decimal(0)
        return max(totals) / grand

    def net_bps(self, round_trip_cost_bps: Decimal) -> Decimal:
        if round_trip_cost_bps < 0:
            raise ValueError(
                f"a round trip cannot be a credit: {round_trip_cost_bps}"
            )
        return self.gross_bps - round_trip_cost_bps


def compute(trades: list[ResolvedTrade]) -> Expectancy:
    """Pool realised P&L, and describe the sample it came from.

    Equal-weighted per trade, not per entity: the caller sees the per-entity
    breakdown and can judge concentration itself. Weighting by entity here
    would quietly turn one asset with four predictions into a peer of one with
    a hundred.
    """
    if not trades:
        return Expectancy(
            n=0,
            gross_bps=Decimal(0),
            assumed_n=0,
            distinct_horizons=0,
            per_entity=(),
        )

    by_entity: dict[str, list[Decimal]] = {}
    for trade in trades:
        by_entity.setdefault(trade.entity_key, []).append(trade.pnl_bps)

    per_entity = tuple(
        sorted(
            (
                (key, len(pnls), sum(pnls, Decimal(0)) / Decimal(len(pnls)))
                for key, pnls in by_entity.items()
            ),
            key=lambda row: row[2],
            reverse=True,
        )
    )
    total = sum((t.pnl_bps for t in trades), Decimal(0))
    return Expectancy(
        n=len(trades),
        gross_bps=total / Decimal(len(trades)),
        assumed_n=sum(1 for t in trades if t.is_assumed),
        distinct_horizons=len({t.horizon_key for t in trades}),
        per_entity=per_entity,
    )
