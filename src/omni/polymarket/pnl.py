"""Polymarket fee curve and per-trade P&L math.

The Polymarket fee formula (post V2, April 2026):

    fee = C × feeRate × p × (1 − p)

where C is shares traded, feeRate is the category-specific rate, and p is the
match price. The curve peaks at p=0.5 and shrinks toward extremes — a 5-cent
market pays roughly 1/25th the fee of an even-money market.

Maker fees are $0 (with rebates). Taker fees follow the formula above. The
paper trader defaults to **maker-only execution**: a hypothetical resting
limit order at the model's probability. The trade "fills" only if the market
crosses that level before the model's view changes. This is the realistic
best case for an edge of the size Stage A measured.

A taker-mode P&L is also provided for the worst case (every signal crosses
the spread immediately). Comparing maker-only to taker-only bounds the
realistic P&L range.

This module is pure math. No IO, no DB, no network. Tests pin every formula.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Polymarket's documented fee rates by category (post V2). Used as defaults;
# the paper trader can override per-market if Gamma reports the actual rate.
DEFAULT_FEE_RATES: dict[str, float] = {
    "Crypto": 0.07,
    "Sports": 0.05,
    "Politics": 0.04,
    "Finance": 0.04,
    "Tech": 0.04,
    "Economics": 0.05,
    "Culture": 0.05,
    "Weather": 0.05,
    "Geopolitics": 0.00,
    "Other": 0.05,
}

DEFAULT_FEE_RATE = 0.05

_EPS = 1e-9


@dataclass(frozen=True)
class Fill:
    """One round-trip trade's economic outcome.

    All money fields are in USD. `gross_pnl` is the payoff-minus-cost before
    fees; `fee_pnl` is the fee cost (always non-positive); `net_pnl` is what
    the trader actually keeps. A losing trade has negative gross and a fee
    drag on top, so `net_pnl` is more negative than `gross_pnl`.
    """

    direction: str
    entry_price: float
    size_shares: float
    outcome_yes: bool
    fee_rate: float
    taker: bool

    def __post_init__(self) -> None:
        if self.direction not in ("YES", "NO"):
            raise ValueError(f"direction must be 'YES' or 'NO', got {self.direction!r}")
        if not (0.0 - _EPS <= self.entry_price <= 1.0 + _EPS):
            raise ValueError(f"entry_price must be in [0, 1], got {self.entry_price}")
        if self.size_shares <= 0:
            raise ValueError(f"size_shares must be positive, got {self.size_shares}")
        if self.fee_rate < 0:
            raise ValueError(f"fee_rate must not be negative: {self.fee_rate}")
        if not math.isfinite(self.entry_price) or not math.isfinite(self.size_shares):
            raise ValueError(f"non-finite inputs: entry={self.entry_price} size={self.size_shares}")

    @property
    def cost(self) -> float:
        return self.entry_price * self.size_shares

    @property
    def payoff(self) -> float:
        """Dollar payoff at resolution. YES pays $1/share on YES-win, else $0.
        NO is the mirror: pays $1/share on NO-win, else $0, with cost (1-p)
        per share."""
        if self.direction == "YES":
            return self.size_shares if self.outcome_yes else 0.0
        return self.size_shares if not self.outcome_yes else 0.0

    @property
    def gross_pnl(self) -> float:
        return self.payoff - self.cost

    @property
    def fee_pnl(self) -> float:
        """Fee cost. Maker = 0. Taker = C × feeRate × p × (1-p) per the V2
        curve. The fee applies to entry; exit at resolution is at par ($0 or
        $1) where p×(1-p) is zero, so exit fees are zero by the formula."""
        if not self.taker:
            return 0.0
        return -self.size_shares * self.fee_rate * self.entry_price * (1.0 - self.entry_price)

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl + self.fee_pnl

    @property
    def roi_pct(self) -> float:
        """Net P&L as a percentage of cost. A losing trade returns -100%
        (lost the entire stake); a winning YES at 0.5 returns +100%."""
        if self.cost <= 0:
            return 0.0
        return self.net_pnl / self.cost * 100.0


def fee_for(
    size_shares: float,
    price: float,
    *,
    fee_rate: float = DEFAULT_FEE_RATE,
    taker: bool = True,
) -> float:
    """The taker fee for a notional of `size_shares` at `price`. Maker = 0."""
    if not taker:
        return 0.0
    if size_shares < 0:
        raise ValueError(f"size_shares must not be negative: {size_shares}")
    if not (0.0 <= price <= 1.0):
        raise ValueError(f"price must be in [0, 1], got {price}")
    return size_shares * fee_rate * price * (1.0 - price)


def size_for_notional(notional_usd: float, price: float) -> float:
    """How many shares a fixed-dollar stake buys at `price` per share."""
    if notional_usd <= 0:
        raise ValueError(f"notional_usd must be positive: {notional_usd}")
    if not (0.0 < price <= 1.0):
        raise ValueError(f"price must be in (0, 1], got {price}")
    return notional_usd / price


@dataclass(frozen=True)
class PnLSummary:
    """Aggregate P&L over a set of closed fills. Reports the metrics a trader
    actually reads: net dollars, win rate, average ROI, and the maker-vs-taker
    spread (the gap between best-case and worst-case execution)."""

    n_closed: int
    n_wins: int
    gross_pnl: float
    fee_pnl: float
    net_pnl: float
    avg_roi_pct: float | None
    worst_drawdown_usd: float

    @property
    def win_rate(self) -> float | None:
        if self.n_closed == 0:
            return None
        return self.n_wins / self.n_closed


def summarise(fills: list[Fill]) -> PnLSummary:
    """Aggregate a list of closed fills into a P&L summary.

    `worst_drawdown_usd` is the largest peak-to-trough decline in cumulative
    net P&L, treating the fills in list order. With n=0 the summary is all
    zeros and `avg_roi_pct` is None — same honest-refusal rule the rest of
    the polymarket module uses.
    """
    if not fills:
        return PnLSummary(
            n_closed=0, n_wins=0,
            gross_pnl=0.0, fee_pnl=0.0, net_pnl=0.0,
            avg_roi_pct=None, worst_drawdown_usd=0.0,
        )

    gross = sum(f.gross_pnl for f in fills)
    fees = sum(f.fee_pnl for f in fills)
    net = sum(f.net_pnl for f in fills)
    wins = sum(1 for f in fills if f.net_pnl > 0)
    rois = [f.roi_pct for f in fills if f.cost > 0]
    avg_roi = sum(rois) / len(rois) if rois else None

    cumulative = 0.0
    peak = 0.0
    worst_dd = 0.0
    for f in fills:
        cumulative += f.net_pnl
        peak = max(peak, cumulative)
        worst_dd = max(worst_dd, peak - cumulative)

    return PnLSummary(
        n_closed=len(fills), n_wins=wins,
        gross_pnl=gross, fee_pnl=fees, net_pnl=net,
        avg_roi_pct=avg_roi, worst_drawdown_usd=worst_dd,
    )


__all__ = [
    "DEFAULT_FEE_RATE",
    "DEFAULT_FEE_RATES",
    "Fill",
    "PnLSummary",
    "fee_for",
    "size_for_notional",
    "summarise",
]
