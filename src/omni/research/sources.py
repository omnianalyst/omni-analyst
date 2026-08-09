"""Declare what data you want; get back an aligned panel you can trust.

`harness.py` is the measurement. This is the plumbing underneath it, and the
plumbing was four fifths of the work six agents each redid: fetch, page, retry,
cache, align to a common date grid, filter a universe, stamp point-in-time.

Everything here exists because it broke something on 2026-08-09:

1. **History depth is probed, never assumed.** Binance's 30-day REST cap on
   positioning metrics is real, and `data.binance.vision` serves 5.9 years of
   the identical series; Bybit serves 5.9 years of open interest. Both signals
   were called unmeasurable from a documented limit and both were measurable.
   `coverage()` reports what a source ACTUALLY holds -- for the archive that is
   a listing of the files that exist, which is a fact rather than a claim.
2. **A walk that cannot finish raises.** Returning what it managed is
   indistinguishable from "this asset has little history", and that silently
   dropped ETH and SOL -- whose history matches BTC's -- out of a measured
   universe. Truncation impersonates exactly the thing being measured.
3. **Point-in-time is structural, not a convention.** The panel is indexed by
   **knowledge date**, so every value on row D was knowable at D. A daily bar
   is stamped with its OPEN and its close does not exist until the bar closes
   (`exchanges.py::parse_ohlcv`), so bar D's close lands on row D+1. Indexing by
   event date instead put a full day of lookahead into a two-day horizon, on the
   entry price and the signal both, and nothing raised.
4. **Nothing forward-fills unless asked, and never past the last print.** An
   unbounded hold resurrects a delisted asset at a frozen price forever, which
   is the survivorship bug wearing a fill policy's clothes.
5. **The universe filter is a stated policy.** Liquidity floor, minimum
   history, minimum asset count -- one object, reused, reproducible, rather than
   a magic number per script.
6. **Survivorship is declared.** Where a venue lets you recover delisted
   symbols the source does it; where it does not, the source says so, because a
   universe of things alive today systematically flatters a long leg.

    source = ClaimStoreSource(field="close", claim_source="binance")
    panel = await source.panel(start=..., end=..., universe=Universe(min_assets=10))
    verdicts = evaluate(name=..., source=..., signal=..., prices=panel.frame)

`panel.frame` is float, not `Decimal`: it feeds numpy through `harness.evaluate`,
which is float end to end. Decimal still rules everywhere money is accrued or
settled -- this frame is a measurement input, and it never reaches a ledger.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Literal, TypeVar
from uuid import UUID

import numpy as np
import pandas as pd

from omni.coverage.visibility import visible_claims_cte
from omni.ingest.exchanges import bar_duration_for
from omni.ingest.protocol import Unavailable

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Crypto trades every calendar day, so the common grid is every calendar day.
#: A venue that closes at weekends would need a different one and none of the
#: sources here is such a venue.
GRID = "D"

DEFAULT_ATTEMPTS = 8
DEFAULT_BACKOFF = 1.2

#: A walk needing more pages than this is not paging, it is looping. Same
#: reasoning and same number as `funding.py`: far past any venue's inception and
#: still a bound.
MAX_PAGES = 400

#: Bitcoin's genesis block, and the `since` a history probe asks from. Not zero:
#: bybit treats `since=0` as "no since given" and answers with its NEWEST bar, so
#: probing with zero reported BTC/USDT as having one day of history on a venue
#: that has served it since 2021-07-05. Every venue here postdates 2009, so a
#: real timestamp older than all of them is honoured where a sentinel is not.
PROBE_SINCE_MS = int(datetime(2009, 1, 3, tzinfo=UTC).timestamp() * 1000)

OBSERVATION_COLUMNS = ("asset", "event_date", "knowledge_date", "value")

#: ccxt's normalised OHLCV row order. Named rather than indexed at the call site
#: because an off-by-one here reads volume as a price and nothing raises.
OHLCV_FIELDS = {"open": 1, "high": 2, "low": 3, "close": 4, "volume": 5}

Reduce = Literal["last", "mean", "sum"]
Fill = Literal["none", "hold"]


class Survivorship(str, Enum):
    """What a source can say about the assets that are no longer listed.

    Stated on every panel rather than left to the caller's memory. A backtest
    run over survivors alone has a long leg selected on having survived, and the
    error is invisible in the output -- it looks like a good result.
    """

    RECOVERS_DELISTED = "recovers_delisted"
    SURVIVORS_ONLY = "survivors_only"
    AS_INGESTED = "as_ingested"

    @property
    def caveat(self) -> str:
        if self is Survivorship.RECOVERS_DELISTED:
            return (
                "delisted symbols are recovered from the venue's archive, so the "
                "universe is the one that existed at each date"
            )
        if self is Survivorship.SURVIVORS_ONLY:
            return (
                "this venue lists only what trades today, so every asset in this "
                "panel survived to the end of the sample; a long leg selected on "
                "survival is flattered and the size of that is not measurable here"
            )
        return (
            "the panel holds what was ingested, including assets whose prints "
            "stopped, but an asset never ingested cannot appear; survivorship is "
            "as good as the ingest's own universe and no better"
        )


@dataclass(frozen=True)
class Span:
    """How much history a source actually holds for one asset, and how it knows.

    `probe` names the method, because "assumed from the documentation" and
    "listed the files that exist" are not the same evidence and one of them was
    wrong twice in a day.
    """

    asset: str
    start: pd.Timestamp
    end: pd.Timestamp
    observations: int | None
    probe: str

    @property
    def days(self) -> int:
        return int((self.end - self.start).days) + 1


@dataclass(frozen=True)
class Universe:
    """A liquidity and history policy, stated once and reused.

    `min_assets` is a refusal, not a preference. A cross-sectional statistic
    computed over six names is not a small result, it is not a result, and the
    harness's own floor is ten.
    """

    min_history_days: int = 0
    min_liquidity: Decimal | None = None
    min_assets: int = 10
    max_assets: int | None = None

    def __post_init__(self) -> None:
        if self.min_history_days < 0:
            raise ValueError("min_history_days cannot be negative")
        if self.min_assets < 1:
            raise ValueError("min_assets must be at least 1")
        if self.max_assets is not None and self.max_assets < self.min_assets:
            raise ValueError(
                f"max_assets {self.max_assets} is below min_assets {self.min_assets}; "
                f"the policy can never be satisfied"
            )
        if self.min_liquidity is not None and not isinstance(self.min_liquidity, Decimal):
            raise TypeError("min_liquidity must be a Decimal so the floor is exact")


@dataclass(frozen=True)
class Panel:
    """An aligned panel plus everything needed to judge whether to believe it.

    `frame` is what `harness.evaluate` takes: index = date, columns = asset,
    values = the field. Its index is the KNOWLEDGE date -- see the module
    docstring -- so a signal reading `frame.loc[:d]` cannot see the future by
    construction rather than by the caller remembering to shift.
    """

    frame: pd.DataFrame
    field: str
    source: str
    survivorship: Survivorship
    coverage: dict[str, Span]
    dropped: dict[str, str]
    held_cells: int
    warnings: tuple[str, ...]

    @property
    def assets(self) -> tuple[str, ...]:
        return tuple(self.frame.columns)

    def summary(self) -> str:
        if self.frame.empty:
            return f"{self.source}.{self.field}: empty"
        return (
            f"{self.source}.{self.field}: {len(self.frame.columns)} assets x "
            f"{len(self.frame.index)} days "
            f"{self.frame.index[0].date()} -> {self.frame.index[-1].date()} | "
            f"{len(self.dropped)} dropped | survivorship {self.survivorship.value}"
        )


class Cache:
    """Disk cache keyed by source plus every parameter that shapes the pull.

    Holds the RAW observations, before alignment, so re-running with a different
    missing-value policy or `as_of` costs nothing either. Writes are atomic: an
    interrupted write leaves a `.tmp` behind and never a half-written entry that
    would read back as a complete but short series -- the exact failure this
    module exists to prevent, arriving through the cache instead of the network.
    """

    def __init__(self, root: Path | str | None = None) -> None:
        if root is None:
            root = os.environ.get("OMNI_RESEARCH_CACHE") or (
                Path.home() / ".cache" / "omni-research"
            )
        self.root = Path(root)

    @staticmethod
    def key(source: str, params: Mapping[str, Any]) -> str:
        payload = json.dumps(
            {"source": source, "params": params}, sort_keys=True, default=str
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def path(self, source: str, params: Mapping[str, Any]) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", source)
        return self.root / f"{safe}-{self.key(source, params)[:32]}.pkl"

    def load(self, source: str, params: Mapping[str, Any]) -> pd.DataFrame | None:
        path = self.path(source, params)
        if not path.exists():
            return None
        try:
            frame = pd.read_pickle(path)
        except Exception:
            logger.warning("unreadable cache entry %s; refetching", path)
            return None
        return frame if isinstance(frame, pd.DataFrame) else None

    def store(self, source: str, params: Mapping[str, Any], frame: pd.DataFrame) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(source, params)
        tmp = path.with_suffix(".tmp")
        frame.to_pickle(tmp)
        os.replace(tmp, path)
        return path


async def persist(
    call: Callable[[], Awaitable[T]],
    *,
    what: str,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF,
    fatal: tuple[type[BaseException], ...] = (),
) -> T:
    """Retry with backoff, then raise. Never return what it managed to get.

    `fatal` names the exceptions that mean the request was wrong rather than
    unlucky -- a symbol the venue does not list will not start existing on the
    fourth attempt, and retrying it eight times just delays an honest refusal.
    """
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return await call()
        except fatal:
            raise
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts and backoff > 0:
                await asyncio.sleep(backoff * (attempt + 1))
    raise Unavailable(
        f"{what} did not complete after {attempts} attempts ({last}); returning the "
        f"rows already collected would be indistinguishable from an asset with a "
        f"short history, so this raises instead"
    ) from last


def _as_utc(moment: datetime | pd.Timestamp | str | None) -> pd.Timestamp | None:
    if moment is None:
        return None
    stamp = pd.Timestamp(moment)
    if stamp.tzinfo is None:
        raise ValueError(
            f"{moment!r} is naive; a naive bound is read as local time and would "
            f"silently shift the requested window"
        )
    return stamp.tz_convert(UTC)


def _knowledge_day(series: pd.Series) -> pd.Series:
    """The calendar day an observation became knowable, as a naive Timestamp.

    Naive to match the grid `harness.evaluate` is fed elsewhere in this project
    (`pd.date_range(..., freq="D")`); the conversion to UTC happens first, so the
    day boundary is UTC's and not the machine's.
    """
    return pd.to_datetime(series, utc=True).dt.floor(GRID).dt.tz_localize(None)


def validate_observations(frame: pd.DataFrame, *, source: str) -> pd.DataFrame:
    """Refuse a frame that cannot be point-in-time correct.

    `knowledge_date < event_date` says a fact was knowable before it happened.
    Nothing downstream can detect it -- the rows look ordinary and a replay
    quietly sees the future -- so it is caught at the boundary.
    """
    missing = [c for c in OBSERVATION_COLUMNS if c not in frame.columns]
    if missing:
        raise Unavailable(f"{source} produced observations without {missing}")
    if frame.empty:
        return frame
    event = pd.to_datetime(frame["event_date"], utc=True)
    knowledge = pd.to_datetime(frame["knowledge_date"], utc=True)
    early = knowledge < event
    if bool(early.any()):
        first = frame.loc[early].iloc[0]
        raise Unavailable(
            f"{source} stamped {first['asset']} knowable at "
            f"{first['knowledge_date']} for an event at {first['event_date']}; a "
            f"fact cannot be known before it happens and a replay would see the "
            f"future without raising"
        )
    return frame


def align(
    observations: pd.DataFrame,
    *,
    as_of: datetime | pd.Timestamp | None = None,
    reduce: Reduce = "last",
    column: str = "value",
) -> pd.DataFrame:
    """Long-form observations to one date index, indexed by KNOWLEDGE date.

    `as_of` drops anything not yet knowable at that instant, which is what makes
    a replay honest; the panel is then reindexed onto a complete calendar grid so
    every asset shares one index and a gap reads as a gap rather than as a
    shorter series silently compared against a longer one.

    `reduce` decides what a day holding several observations of one asset means
    -- funding settles three times a day, positioning metrics every five minutes
    -- and it is explicit because taking one of them silently is a decision the
    caller should have made.
    """
    if observations.empty:
        return pd.DataFrame()

    frame = observations.copy()
    cutoff = _as_utc(as_of)
    if cutoff is not None:
        knowable = pd.to_datetime(frame["knowledge_date"], utc=True) <= cutoff
        frame = frame.loc[knowable]
        if frame.empty:
            return pd.DataFrame()

    frame["_day"] = _knowledge_day(frame["knowledge_date"])
    frame = frame.sort_values(["_day", "knowledge_date", "event_date"])

    if reduce == "last":
        collapsed = frame.drop_duplicates(subset=["_day", "asset"], keep="last")
        wide = collapsed.pivot(index="_day", columns="asset", values=column)
    elif reduce in ("mean", "sum"):
        wide = frame.pivot_table(
            index="_day", columns="asset", values=column, aggfunc=reduce
        )
    else:
        raise ValueError(f"unknown reduce {reduce!r}; use last, mean or sum")

    grid = pd.date_range(wide.index.min(), wide.index.max(), freq=GRID)
    wide = wide.reindex(index=grid)
    wide.index.name = None
    wide.columns.name = None
    return wide.astype(float).sort_index(axis=1)


def apply_fill(frame: pd.DataFrame, *, fill: Fill, hold_limit: int | None) -> tuple[pd.DataFrame, int]:
    """Carry a stale value forward only when asked, only so far, and never past
    the last real print.

    An unbounded hold is refused rather than defaulted, because a hold with no
    limit keeps a delisted asset in the cross-section at a frozen price for the
    rest of the sample: it contributes a zero return to whichever leg holds it,
    which is not what happened to anyone who owned it.

    Filling stops at each asset's last observation for the same reason. Interior
    gaps are a venue's missing print; the tail is a delisting.
    """
    if fill == "none":
        return frame, 0
    if fill != "hold":
        raise ValueError(f"unknown fill {fill!r}; use none or hold")
    if hold_limit is None or hold_limit < 1:
        raise ValueError(
            "fill='hold' requires an explicit hold_limit of at least 1 day; an "
            "unbounded hold carries a delisted asset at a frozen price to the end "
            "of the sample and reads as a flat return rather than as an absence"
        )

    filled = frame.ffill(limit=hold_limit)
    for asset in frame.columns:
        last = frame[asset].last_valid_index()
        if last is None:
            continue
        filled.loc[filled.index > last, asset] = np.nan
    synthesized = int((filled.notna() & frame.isna()).to_numpy().sum())
    return filled, synthesized


def _median_liquidity(liquidity: pd.DataFrame, asset: str) -> Decimal | None:
    column = liquidity[asset].dropna() if asset in liquidity.columns else pd.Series(dtype=float)
    if column.empty:
        return None
    value = float(column.median())
    if not np.isfinite(value):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def apply_universe(
    frame: pd.DataFrame,
    *,
    policy: Universe,
    liquidity: pd.DataFrame | None,
    source: str,
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Filter to the tradeable cross-section, reproducibly, or refuse.

    Reproducible means the same policy over the same data yields the same
    columns in the same order every time: ranking ties break on the symbol, and
    the survivors come back sorted. A ranking that depends on dict order gives a
    different universe to the same script on the same day.

    Asking for a liquidity floor from a source that cannot measure liquidity is
    an error, not a filter that quietly passes everything. That silent pass is
    how a "top 50 by volume" universe becomes "every symbol".
    """
    ranked = policy.min_liquidity is not None or policy.max_assets is not None
    if ranked and liquidity is None:
        raise Unavailable(
            f"{source} publishes no liquidity series, so a liquidity floor or a "
            f"top-N cut cannot be applied; passing every asset through would "
            f"silently widen the universe the policy names"
        )

    dropped: dict[str, str] = {}
    kept: list[str] = []
    ranks: dict[str, Decimal] = {}

    for asset in sorted(frame.columns):
        observed = int(frame[asset].notna().sum())
        if observed < policy.min_history_days:
            dropped[asset] = f"{observed} observations, below the {policy.min_history_days} floor"
            continue
        if ranked and liquidity is not None:
            median = _median_liquidity(liquidity, asset)
            if median is None:
                dropped[asset] = "no liquidity observations in the window"
                continue
            ranks[asset] = median
            if policy.min_liquidity is not None and median < policy.min_liquidity:
                dropped[asset] = f"median liquidity {median} below the {policy.min_liquidity} floor"
                continue
        kept.append(asset)

    if policy.max_assets is not None and len(kept) > policy.max_assets:
        ordered = sorted(kept, key=lambda a: (-ranks[a], a))
        for asset in ordered[policy.max_assets:]:
            dropped[asset] = f"outside the top {policy.max_assets} by median liquidity"
        kept = ordered[: policy.max_assets]

    if len(kept) < policy.min_assets:
        raise Unavailable(
            f"{source}: {len(kept)} assets survived the universe policy, below the "
            f"{policy.min_assets} floor; a cross-sectional statistic over that many "
            f"names is not a small result, it is not a result. Dropped: "
            f"{sorted(dropped.items())}"
        )
    return frame[sorted(kept)], dropped


class Source:
    """Declare a series; receive an aligned, point-in-time, cached panel.

    Subclasses implement three things: how to resolve the asset list, how to
    probe what history actually exists, and how to fetch observations. Alignment,
    caching, missing-value policy and universe filtering are here, once, because
    six agents writing them six times is what this module replaces.
    """

    name: str = "source"
    survivorship: Survivorship = Survivorship.SURVIVORS_ONLY

    def __init__(
        self,
        *,
        field: str,
        cache: Cache | None = None,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff: float = DEFAULT_BACKOFF,
    ) -> None:
        self.field = field
        self._cache = Cache() if cache is None else cache
        self._attempts = attempts
        self._backoff = backoff

    @property
    def liquidity_field(self) -> str | None:
        return None

    async def resolve(self, assets: Sequence[str] | None) -> tuple[str, ...]:
        if assets is None:
            raise Unavailable(f"{self.name} cannot enumerate its own assets; name them")
        return tuple(sorted(dict.fromkeys(assets)))

    async def coverage(self, assets: Sequence[str] | None = None) -> dict[str, Span]:
        raise NotImplementedError

    async def _observe(
        self,
        assets: tuple[str, ...],
        *,
        start: pd.Timestamp | None,
        end: pd.Timestamp | None,
    ) -> pd.DataFrame:
        raise NotImplementedError

    def _cache_params(
        self,
        assets: tuple[str, ...],
        *,
        start: pd.Timestamp | None,
        end: pd.Timestamp | None,
    ) -> dict[str, Any]:
        return {
            "field": self.field,
            "liquidity_field": self.liquidity_field,
            "assets": list(assets),
            "start": None if start is None else start.isoformat(),
            "end": None if end is None else end.isoformat(),
        }

    async def observations(
        self,
        assets: Sequence[str] | None = None,
        *,
        start: datetime | pd.Timestamp | None = None,
        end: datetime | pd.Timestamp | None = None,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        resolved = await self.resolve(assets)
        lo, hi = _as_utc(start), _as_utc(end)
        params = self._cache_params(resolved, start=lo, end=hi)
        if use_cache:
            hit = self._cache.load(self.name, params)
            if hit is not None:
                return hit
        # Stored only after the walk returned, so a pull that raised leaves
        # nothing behind to be served as complete on the next run.
        frame = validate_observations(
            await self._observe(resolved, start=lo, end=hi), source=self.name
        )
        if use_cache:
            self._cache.store(self.name, params, frame)
        return frame

    async def panel(
        self,
        assets: Sequence[str] | None = None,
        *,
        start: datetime | pd.Timestamp | None = None,
        end: datetime | pd.Timestamp | None = None,
        as_of: datetime | pd.Timestamp | None = None,
        universe: Universe | None = None,
        fill: Fill = "none",
        hold_limit: int | None = None,
        reduce: Reduce = "last",
        use_cache: bool = True,
    ) -> Panel:
        policy = Universe() if universe is None else universe
        raw = await self.observations(assets, start=start, end=end, use_cache=use_cache)
        if raw.empty:
            raise Unavailable(
                f"{self.name} returned no observations for {self.field} in the "
                f"requested window; an empty panel is an absence, not a result"
            )

        frame = align(raw, as_of=as_of, reduce=reduce)
        liquidity = (
            align(raw, as_of=as_of, reduce=reduce, column="liquidity")
            if self.liquidity_field is not None and "liquidity" in raw.columns
            else None
        )

        frame, held = apply_fill(frame, fill=fill, hold_limit=hold_limit)
        frame, dropped = apply_universe(
            frame, policy=policy, liquidity=liquidity, source=self.name
        )
        if liquidity is not None:
            liquidity = liquidity.reindex(index=frame.index, columns=frame.columns)

        spans = {
            asset: Span(
                asset=asset,
                start=frame[asset].first_valid_index(),
                end=frame[asset].last_valid_index(),
                observations=int(frame[asset].notna().sum()),
                probe=f"{self.name} panel",
            )
            for asset in frame.columns
            if frame[asset].first_valid_index() is not None
        }

        warnings = [f"survivorship: {self.survivorship.caveat}"]
        if held:
            warnings.append(
                f"{held} cells carried forward under fill='hold' with a "
                f"{hold_limit}-day limit; they are not prints"
            )
        gaps = int(frame.isna().to_numpy().sum())
        if gaps:
            warnings.append(
                f"{gaps} of {frame.size} cells are absent and stay NaN; the harness "
                f"skips an asset at any period whose endpoints are not both present"
            )

        return Panel(
            frame=frame,
            field=self.field,
            source=self.name,
            survivorship=self.survivorship,
            coverage=spans,
            dropped=dropped,
            held_cells=held,
            warnings=tuple(warnings),
        )


CLAIM_COVERAGE_SQL = """
WITH visible AS (
{visible}
)
SELECT e.symbol AS asset,
       min(c.event_date) AS first_event,
       max(c.event_date) AS last_event,
       count(*)          AS observations
FROM visible c
JOIN entity e ON e.id = c.entity_id
WHERE c.claim_type = $2::claim_type
  AND ($3::text IS NULL OR c.source = $3)
  AND e.symbol IS NOT NULL
GROUP BY e.symbol
ORDER BY e.symbol
"""

CLAIM_ROWS_SQL = """
WITH visible AS (
{visible}
)
SELECT e.symbol AS asset, c.event_date, c.knowledge_date, c.value
FROM visible c
JOIN entity e ON e.id = c.entity_id
WHERE c.claim_type = $2::claim_type
  AND ($3::text IS NULL OR c.source = $3)
  AND ($4::text[] IS NULL OR e.symbol = ANY($4))
  AND ($5::timestamptz IS NULL OR c.event_date >= $5)
  AND ($6::timestamptz IS NULL OR c.event_date <= $6)
ORDER BY e.symbol, c.event_date, c.knowledge_date
"""


class ClaimStoreSource(Source):
    """This project's own claim store: the cheapest source and the most used.

    Reads through `visible_claims_cte`, so the audience rule is the same one the
    rest of the system enforces: a user sees the shared network plus their own
    `byo_only` claims and nothing else. Bypassing it with a plain join would make
    a research panel the path by which one operator's licensed data reaches
    another, which the provider terms forbid and which no test downstream of here
    would notice.

    The bitemporal pair is taken from the claim, not reconstructed: `event_date`
    is when it happened, `knowledge_date` when it became knowable, and every
    adapter that writes here has already made that distinction correctly.
    """

    name = "claim_store"
    survivorship = Survivorship.AS_INGESTED

    def __init__(
        self,
        *,
        field: str = "close",
        claim_type: str = "price_snapshot",
        claim_source: str | None = None,
        liquidity_field: str | None = None,
        audience: UUID | None = None,
        pool: Any = None,
        database_url: str | None = None,
        cache: Cache | None = None,
    ) -> None:
        super().__init__(field=field, cache=cache)
        self._claim_type = claim_type
        self._claim_source = claim_source
        self._liquidity_field = liquidity_field
        self._audience = audience
        self._pool = pool
        self._database_url = database_url
        self.name = f"claim_store.{claim_type}" + (
            f".{claim_source}" if claim_source else ""
        )

    @property
    def liquidity_field(self) -> str | None:
        return self._liquidity_field

    def _cache_params(self, assets, *, start, end) -> dict[str, Any]:
        params = super()._cache_params(assets, start=start, end=end)
        params.update(
            claim_type=self._claim_type,
            claim_source=self._claim_source,
            audience=None if self._audience is None else str(self._audience),
        )
        return params

    async def _fetch(self, sql: str, *args: Any) -> list[Any]:
        if self._pool is not None:
            return await self._pool.fetch(sql, *args)

        import asyncpg

        from omni.config import settings

        url = self._database_url or settings.database_url
        connection = await asyncpg.connect(url)
        try:
            return await connection.fetch(sql, *args)
        finally:
            await connection.close()

    async def resolve(self, assets: Sequence[str] | None) -> tuple[str, ...]:
        if assets is not None:
            return tuple(sorted(dict.fromkeys(assets)))
        return tuple(sorted(await self.coverage()))

    async def coverage(self, assets: Sequence[str] | None = None) -> dict[str, Span]:
        """Probed by counting the rows that exist, per asset, under the audience
        rule -- so it reports what THIS caller can see, not what the table holds.
        """
        rows = await self._fetch(
            CLAIM_COVERAGE_SQL.format(visible=visible_claims_cte("$1")),
            self._audience,
            self._claim_type,
            self._claim_source,
        )
        wanted = None if assets is None else set(assets)
        spans: dict[str, Span] = {}
        for row in rows:
            asset = row["asset"]
            if wanted is not None and asset not in wanted:
                continue
            spans[asset] = Span(
                asset=asset,
                start=pd.Timestamp(row["first_event"]).tz_convert(UTC),
                end=pd.Timestamp(row["last_event"]).tz_convert(UTC),
                observations=int(row["observations"]),
                probe="claim store row count, audience-scoped",
            )
        return spans

    async def _observe(self, assets, *, start, end) -> pd.DataFrame:
        rows = await self._fetch(
            CLAIM_ROWS_SQL.format(visible=visible_claims_cte("$1")),
            self._audience,
            self._claim_type,
            self._claim_source,
            list(assets) or None,
            None if start is None else start.to_pydatetime(),
            None if end is None else end.to_pydatetime(),
        )

        records: list[dict[str, Any]] = []
        unusable = 0
        for row in rows:
            value = row["value"]
            if isinstance(value, (str, bytes)):
                value = json.loads(value)
            if not isinstance(value, dict):
                unusable += 1
                continue
            scalar = _finite(value.get(self.field))
            if scalar is None:
                unusable += 1
                continue
            record = {
                "asset": row["asset"],
                "event_date": row["event_date"],
                "knowledge_date": row["knowledge_date"],
                "value": scalar,
            }
            if self._liquidity_field is not None:
                record["liquidity"] = _finite(value.get(self._liquidity_field))
            records.append(record)

        if unusable:
            logger.info(
                "%s: %d claims carried no usable %r and were skipped rather than "
                "zero-filled",
                self.name,
                unusable,
                self.field,
            )
        return pd.DataFrame.from_records(records, columns=_columns(self._liquidity_field))


def _finite(value: Any) -> float | None:
    """A number, or an honest absence. Never a substituted zero.

    Zero is a real observation -- a zero-volume bar happens -- so it passes, and
    the check is on finiteness rather than on truthiness for exactly that reason.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if np.isfinite(parsed) else None


def _columns(liquidity_field: str | None) -> list[str]:
    return list(OBSERVATION_COLUMNS) + (["liquidity"] if liquidity_field else [])


class CCXTOhlcvSource(Source):
    """Daily OHLCV from any ccxt venue, paged and never truncated.

    History depth is probed by asking the venue for its own earliest bar rather
    than trusting a documented limit. That probe is honest about the venue's REST
    surface and it is not the last word on what exists: Binance's REST answer for
    positioning metrics is 30 days and its archive holds 5.9 years of the same
    series. When the probe returns much less than requested the panel says so
    instead of quietly starting late.

    A bar is stamped with its OPEN and its close is not knowable until the bar
    closes, so `knowledge_date = event_date + bar_duration`. This is the same
    correction `exchanges.py::parse_ohlcv` carries and the same lookahead it
    fixed.
    """

    name = "ccxt"
    survivorship = Survivorship.SURVIVORS_ONLY

    def __init__(
        self,
        *,
        venue: str,
        field: str = "close",
        timeframe: str = "1d",
        liquidity: Literal["volume", "quote_volume"] | None = None,
        page_limit: int = 1000,
        fetch_fn: Callable[..., Awaitable[list[list[Any]]]] | None = None,
        cache: Cache | None = None,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff: float = DEFAULT_BACKOFF,
    ) -> None:
        super().__init__(field=field, cache=cache, attempts=attempts, backoff=backoff)
        if not venue or not venue.strip():
            raise ValueError("venue must be named; it is half the identity of a bar")
        if field not in OHLCV_FIELDS:
            raise ValueError(
                f"unknown OHLCV field {field!r}; ccxt rows carry "
                f"{sorted(OHLCV_FIELDS)} and guessing an index would silently read "
                f"the wrong column"
            )
        self._venue = venue
        self._timeframe = timeframe
        self._bar = bar_duration_for(timeframe)
        self._liquidity = liquidity
        self._page_limit = page_limit
        self._fetch_fn = fetch_fn
        self._exchange: Any = None
        self.name = f"ccxt.{venue}.{timeframe}"

    @property
    def liquidity_field(self) -> str | None:
        return self._liquidity

    def _cache_params(self, assets, *, start, end) -> dict[str, Any]:
        params = super()._cache_params(assets, start=start, end=end)
        params.update(venue=self._venue, timeframe=self._timeframe)
        return params

    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[None]:
        """One exchange per operation, closed on the way out.

        ccxt's async exchanges own an aiohttp session; leaving one open leaks a
        connector and the warning it prints is easy to miss in a research script.
        `enableRateLimit` is ccxt's own throttle, which knows each venue's
        per-endpoint weights -- a fixed sleep here would not.
        """
        if self._fetch_fn is not None:
            yield
            return
        import ccxt.async_support as ccxt_async

        try:
            exchange_cls = getattr(ccxt_async, self._venue)
        except AttributeError as exc:
            raise Unavailable(f"ccxt has no venue named {self._venue!r}") from exc
        exchange = exchange_cls({"enableRateLimit": True})
        self._exchange = exchange
        try:
            yield
        finally:
            self._exchange = None
            await exchange.close()

    def _fatal(self) -> tuple[type[BaseException], ...]:
        """Exceptions that mean the request was wrong, not that the venue was busy.

        A venue that does not list a symbol will not start listing it on the
        fourth attempt, and retrying eight times only delays an honest refusal.
        """
        try:
            import ccxt
        except ImportError:
            return ()
        return (ccxt.BadSymbol,)

    async def _page(
        self, symbol: str, *, since: int | None, limit: int | None
    ) -> list[list[Any]]:
        if self._fetch_fn is not None:
            return await self._fetch_fn(symbol, since=since, limit=limit)
        if self._exchange is None:
            raise Unavailable(f"{self.name} used outside a session")
        return await self._exchange.fetch_ohlcv(
            symbol, self._timeframe, since=since, limit=limit
        )

    async def coverage(self, assets: Sequence[str] | None = None) -> dict[str, Span]:
        """The venue's own earliest and latest bar, asked for rather than assumed.

        The probe checks that it was honoured. `since=0` reads as "no since
        given" on bybit, which answers with its newest bar -- so a probe that
        trusted the reply reported BTC/USDT as one day old on a venue that has
        served it since 2021. A probe whose answer lands within one bar of the
        newest bar is refused rather than reported, because a fabricated depth of
        one day is worse than no answer: it looks like a young asset, which is
        the quantity the caller is trying to measure.

        This probes the REST surface, and the REST surface is not the whole
        truth: Binance answers 30 days for positioning metrics while its archive
        holds 5.9 years of the same series. `probe` therefore names what was
        asked, so a shortfall against the requested window is visible here rather
        than discovered later as a short panel.
        """
        resolved = await self.resolve(assets)
        fatal = self._fatal()
        spans: dict[str, Span] = {}
        async with self._session():
            for symbol in resolved:
                first = await persist(
                    lambda s=symbol: self._page(s, since=PROBE_SINCE_MS, limit=1),
                    what=f"{self.name} earliest bar for {symbol}",
                    attempts=self._attempts,
                    backoff=self._backoff,
                    fatal=fatal,
                )
                latest = await persist(
                    lambda s=symbol: self._page(s, since=None, limit=1),
                    what=f"{self.name} latest bar for {symbol}",
                    attempts=self._attempts,
                    backoff=self._backoff,
                    fatal=fatal,
                )
                if not first or not latest:
                    continue
                start = pd.Timestamp(int(first[0][0]), unit="ms", tz=UTC)
                end = pd.Timestamp(int(latest[-1][0]), unit="ms", tz=UTC)
                if start >= end - self._bar:
                    raise Unavailable(
                        f"{self._venue} answered a request from "
                        f"{pd.Timestamp(PROBE_SINCE_MS, unit='ms', tz=UTC).date()} with "
                        f"its newest bar for {symbol} ({start}); it is ignoring `since` "
                        f"on this endpoint, so its history depth cannot be probed this "
                        f"way. Reporting one bar of history would be indistinguishable "
                        f"from a newly listed asset"
                    )
                spans[symbol] = Span(
                    asset=symbol,
                    start=start,
                    end=end,
                    observations=None,
                    probe=f"{self._venue} REST earliest and latest bar",
                )
        return spans

    async def _walk(self, symbol: str, *, start: pd.Timestamp, end: pd.Timestamp | None):
        """Page forward until the venue stops advancing, retrying every page.

        Three ends, and only two of them are acceptable: an empty page and a
        cursor that stops advancing both mean the venue has no more to give. The
        third -- the page cap -- means the walk is still advancing and would be
        cut short, so it raises. `funding.py` learned this when a rate limit
        turned into a short series that looked like a young asset.
        """
        cursor = int(start.timestamp() * 1000)
        ceiling = None if end is None else int(end.timestamp() * 1000)
        collected: dict[int, list[Any]] = {}
        fatal = self._fatal()

        for _ in range(MAX_PAGES):
            page = await persist(
                lambda c=cursor: self._page(symbol, since=c, limit=self._page_limit),
                what=f"{self.name} page for {symbol} from {cursor}",
                attempts=self._attempts,
                backoff=self._backoff,
                fatal=fatal,
            )
            stamped = {
                int(row[0]): row
                for row in page or []
                if isinstance(row, (list, tuple)) and len(row) >= 6 and row[0] is not None
            }
            if not stamped:
                return collected
            newest = max(stamped)
            if newest + 1 <= cursor:
                return collected
            collected.update(stamped)
            cursor = newest + 1
            if ceiling is not None and cursor > ceiling:
                return collected
        raise Unavailable(
            f"{self.name} was still advancing through {symbol} after {MAX_PAGES} "
            f"pages; stopping here would truncate the series and a truncated walk "
            f"is indistinguishable from a short history"
        )

    async def _observe(self, assets, *, start, end) -> pd.DataFrame:
        if start is None:
            raise ValueError(
                f"{self.name} needs an explicit start; without one ccxt returns a "
                f"single page and a single page is a truncation that raises nothing"
            )
        records: list[dict[str, Any]] = []
        async with self._session():
            for symbol in assets:
                rows = await self._walk(symbol, start=start, end=end)
                records.extend(self._records(symbol, rows, end=end))
        return pd.DataFrame.from_records(records, columns=_columns(self._liquidity))

    def _records(
        self,
        symbol: str,
        rows: Mapping[int, Sequence[Any]],
        *,
        end: pd.Timestamp | None,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for ts, row in sorted(rows.items()):
            opened = pd.Timestamp(ts, unit="ms", tz=UTC)
            if end is not None and opened > end:
                continue
            scalar = _finite(row[OHLCV_FIELDS[self.field]])
            if scalar is None:
                continue
            record = {
                "asset": symbol,
                "event_date": opened,
                "knowledge_date": opened + self._bar,
                "value": scalar,
            }
            if self._liquidity is not None:
                volume = _finite(row[OHLCV_FIELDS["volume"]])
                close = _finite(row[OHLCV_FIELDS["close"]])
                if self._liquidity == "volume":
                    record["liquidity"] = volume
                else:
                    # ccxt's normalised OHLCV carries base volume only, so
                    # notional is close x volume -- a stated approximation of
                    # quote volume, not a field the venue published.
                    record["liquidity"] = (
                        None if volume is None or close is None else close * volume
                    )
            records.append(record)
        return records


ARCHIVE_HOST = "https://data.binance.vision"
ARCHIVE_LISTING = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
_KEY_RE = re.compile(r"<Key>([^<]+)</Key>")
_COMMON_PREFIX_RE = re.compile(r"<CommonPrefixes><Prefix>([^<]+)</Prefix></CommonPrefixes>")
_TRUNCATED_RE = re.compile(r"<IsTruncated>true</IsTruncated>")
_NEXT_MARKER_RE = re.compile(r"<NextMarker>([^<]+)</NextMarker>")
_MONTH_RE = re.compile(r"-(\d{4}-\d{2})\.zip$")
_DAY_RE = re.compile(r"-(\d{4}-\d{2}-\d{2})\.zip$")

#: klines CSV column order, per Binance's own archive layout. Written out rather
#: than positional-guessed because the file has no header before 2025 and a
#: shifted index would read taker volume as a price without failing.
KLINE_COLUMNS = {
    "open_time": 0,
    "open": 1,
    "high": 2,
    "low": 3,
    "close": 4,
    "volume": 5,
    "close_time": 6,
    "quote_volume": 7,
    "trades": 8,
    "taker_buy_base": 9,
    "taker_buy_quote": 10,
}


class BinanceArchiveSource(Source):
    """`data.binance.vision`: years of the series the REST API caps at 30 days.

    The highest-leverage source here. Binance publishes the same data it serves
    over REST as daily and monthly zip files, and the S3 bucket is listable, so:

      * **History depth is a fact, not an estimate.** `coverage()` lists the keys
        that exist and reads the dates out of their names. That listing is what
        turned "positioning is unmeasurable past 30 days" into 230,282
        symbol-days pulled with zero errors.
      * **Delisted symbols are recoverable.** The bucket keeps a symbol's files
        after the venue stops listing it -- MATICUSDT's klines are still there --
        so `symbols()` returns the universe that existed, not the one alive
        today. This is the only source here that can say that.
      * **The dataset axis generalises.** `klines` and `metrics` share the layout
        and differ only in path and parse, which is why open interest, top-trader
        ratios and taker flow all arrive through one class.

    A listed key that will not download after every retry RAISES. The reference
    run collected such failures into an error list and continued; for research
    that is a hole in the middle of a series with nothing marking it.
    """

    name = "binance_archive"
    survivorship = Survivorship.RECOVERS_DELISTED

    def __init__(
        self,
        *,
        dataset: Literal["klines", "metrics"] = "klines",
        field: str = "close",
        market: str = "futures/um",
        interval: str = "1d",
        liquidity_field: str | None = None,
        concurrency: int = 24,
        http_get: Callable[[str], Awaitable[bytes]] | None = None,
        cache: Cache | None = None,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff: float = DEFAULT_BACKOFF,
    ) -> None:
        super().__init__(field=field, cache=cache, attempts=attempts, backoff=backoff)
        if dataset not in ("klines", "metrics"):
            raise ValueError(f"unknown dataset {dataset!r}; use klines or metrics")
        self._dataset = dataset
        self._market = market
        self._interval = interval
        self._liquidity_field = liquidity_field
        self._concurrency = concurrency
        self._http_get = http_get
        self.name = f"binance_archive.{market.replace('/', '_')}.{dataset}"

    @property
    def liquidity_field(self) -> str | None:
        return self._liquidity_field

    def _cache_params(self, assets, *, start, end) -> dict[str, Any]:
        params = super()._cache_params(assets, start=start, end=end)
        params.update(dataset=self._dataset, market=self._market, interval=self._interval)
        return params

    def _prefix(self, symbol: str | None = None) -> str:
        if self._dataset == "klines":
            root = f"data/{self._market}/monthly/klines/"
            return root if symbol is None else f"{root}{symbol}/{self._interval}/"
        root = f"data/{self._market}/daily/metrics/"
        return root if symbol is None else f"{root}{symbol}/"

    async def _get(self, url: str) -> bytes:
        if self._http_get is not None:
            return await self._http_get(url)
        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(url, headers={"User-Agent": "omni-research"})
            response.raise_for_status()
            return response.content

    async def _listing(self, prefix: str) -> tuple[list[str], list[str]]:
        """Every key and sub-prefix under `prefix`, following every marker.

        A listing that stops early understates the history that exists, which is
        the assumption this whole source was built to disprove, so the walk
        raises rather than returning the pages it managed.
        """
        import urllib.parse

        keys: list[str] = []
        prefixes: list[str] = []
        marker = ""
        for _ in range(MAX_PAGES):
            url = (
                f"{ARCHIVE_LISTING}?delimiter=/"
                f"&prefix={urllib.parse.quote(prefix)}&max-keys=1000"
            )
            if marker:
                url += "&marker=" + urllib.parse.quote(marker, safe="")
            body = (
                await persist(
                    lambda u=url: self._get(u),
                    what=f"{self.name} listing of {prefix}",
                    attempts=self._attempts,
                    backoff=self._backoff,
                )
            ).decode("utf-8", "replace")

            page_keys = [k for k in _KEY_RE.findall(body) if not k.endswith("CHECKSUM")]
            keys.extend(page_keys)
            prefixes.extend(_COMMON_PREFIX_RE.findall(body))
            if not _TRUNCATED_RE.search(body):
                return keys, prefixes
            found = _NEXT_MARKER_RE.search(body)
            if found is not None:
                marker = found.group(1)
            elif page_keys:
                marker = page_keys[-1]
            else:
                return keys, prefixes
        raise Unavailable(
            f"{self.name} listing of {prefix} was still truncated after {MAX_PAGES} "
            f"pages; a partial listing understates the history that exists"
        )

    async def symbols(self) -> tuple[str, ...]:
        """Every symbol the archive holds, including ones no longer listed.

        This is the survivorship recovery. A universe built from a venue's live
        market list omits everything that died, and the long leg of any
        cross-sectional test over it was selected on having survived.
        """
        _keys, prefixes = await self._listing(self._prefix())
        root = self._prefix()
        return tuple(sorted(p[len(root):].strip("/") for p in prefixes if p.startswith(root)))

    async def resolve(self, assets: Sequence[str] | None) -> tuple[str, ...]:
        if assets is not None:
            return tuple(sorted(dict.fromkeys(assets)))
        return await self.symbols()

    def _key_period(self, key: str) -> str | None:
        pattern = _MONTH_RE if self._dataset == "klines" else _DAY_RE
        found = pattern.search(key)
        return found.group(1) if found else None

    async def _keys_for(self, symbol: str) -> list[tuple[str, str]]:
        keys, _prefixes = await self._listing(self._prefix(symbol))
        out = []
        for key in keys:
            period = self._key_period(key)
            if period is not None:
                out.append((period, key))
        return sorted(out)

    async def coverage(self, assets: Sequence[str] | None = None) -> dict[str, Span]:
        """The files that exist, listed. Not a documented limit, not an estimate."""
        resolved = await self.resolve(assets)
        gate = asyncio.Semaphore(self._concurrency)

        async def one(symbol: str) -> tuple[str, list[tuple[str, str]]]:
            async with gate:
                return symbol, await self._keys_for(symbol)

        spans: dict[str, Span] = {}
        for symbol, keys in await asyncio.gather(*(one(s) for s in resolved)):
            if not keys:
                continue
            first, last = keys[0][0], keys[-1][0]
            spans[symbol] = Span(
                asset=symbol,
                start=pd.Timestamp(first, tz=UTC),
                end=_period_end(last),
                observations=len(keys),
                probe=f"{ARCHIVE_HOST} object listing ({len(keys)} files)",
            )
        return spans

    async def _observe(self, assets, *, start, end) -> pd.DataFrame:
        gate = asyncio.Semaphore(self._concurrency)

        async def one(symbol: str) -> list[dict[str, Any]]:
            keys = [
                key
                for period, key in await self._keys_for(symbol)
                if _period_wanted(period, start, end)
            ]

            async def download(key: str) -> list[dict[str, Any]]:
                async with gate:
                    body = await persist(
                        lambda k=key: self._get(f"{ARCHIVE_HOST}/{k}"),
                        what=f"{self.name} object {key}",
                        attempts=self._attempts,
                        backoff=self._backoff,
                    )
                return self._parse(symbol, key, body)

            rows: list[dict[str, Any]] = []
            for chunk in await asyncio.gather(*(download(k) for k in keys)):
                rows.extend(chunk)
            return rows

        records: list[dict[str, Any]] = []
        for rows in await asyncio.gather(*(one(s) for s in assets)):
            records.extend(rows)

        frame = pd.DataFrame.from_records(records, columns=_columns(self._liquidity_field))
        if not frame.empty and start is not None:
            frame = frame.loc[pd.to_datetime(frame["event_date"], utc=True) >= start]
        if not frame.empty and end is not None:
            frame = frame.loc[pd.to_datetime(frame["event_date"], utc=True) <= end]
        return frame.reset_index(drop=True)

    def _parse(self, symbol: str, key: str, body: bytes) -> list[dict[str, Any]]:
        try:
            archive = zipfile.ZipFile(io.BytesIO(body))
            text = archive.read(archive.namelist()[0]).decode("utf-8", "replace")
        except Exception as exc:
            raise Unavailable(
                f"{self.name}: {key} listed but did not unpack ({exc}); dropping it "
                f"would leave an unmarked hole in {symbol}'s series"
            ) from exc
        if self._dataset == "klines":
            return self._parse_klines(symbol, text)
        return self._parse_metrics(symbol, text)

    def _parse_klines(self, symbol: str, text: str) -> list[dict[str, Any]]:
        """`event_date` is the bar's open, `knowledge_date` its close plus a tick.

        The archive publishes `close_time` explicitly (23:59:59.999 for a daily
        bar), so the moment the bar's close existed is the venue's own statement
        rather than an assumed duration. Bar D therefore lands on row D+1 of the
        panel, which is the whole point.
        """
        index = KLINE_COLUMNS.get(self.field)
        if index is None:
            raise ValueError(
                f"unknown klines field {self.field!r}; the archive publishes "
                f"{sorted(KLINE_COLUMNS)}"
            )
        liquidity_index = (
            None if self._liquidity_field is None else KLINE_COLUMNS.get(self._liquidity_field)
        )
        if self._liquidity_field is not None and liquidity_index is None:
            raise ValueError(f"unknown klines liquidity field {self._liquidity_field!r}")

        out: list[dict[str, Any]] = []
        for line in text.splitlines():
            parts = line.split(",")
            if len(parts) < 11 or not parts[0].strip().isdigit():
                continue
            opened = _epoch(parts[0])
            closed = _epoch(parts[6])
            scalar = _finite(parts[index])
            if opened is None or closed is None or scalar is None:
                continue
            record = {
                "asset": symbol,
                "event_date": opened,
                "knowledge_date": closed + pd.Timedelta(1, unit="ms"),
                "value": scalar,
            }
            if liquidity_index is not None:
                record["liquidity"] = _finite(parts[liquidity_index])
            out.append(record)
        return out

    def _parse_metrics(self, symbol: str, text: str) -> list[dict[str, Any]]:
        """A metrics row is a snapshot, so it is knowable the instant it is taken.

        `knowledge_date == event_date` here and that is not the klines case: a bar
        describes an interval that has to finish, a snapshot describes the moment
        it was taken. Stamping a snapshot later would hide it from a replay that
        could legitimately have seen it.
        """
        lines = text.strip().splitlines()
        if not lines or not lines[0].startswith("create_time"):
            raise Unavailable(
                f"{self.name}: {symbol} metrics file has no create_time header; its "
                f"columns cannot be located and a positional guess would read the "
                f"wrong series"
            )
        header = [c.strip() for c in lines[0].split(",")]
        try:
            index = header.index(self.field)
        except ValueError:
            raise Unavailable(
                f"{self.name}: {symbol} metrics file has no column {self.field!r}; "
                f"it publishes {header}"
            ) from None
        liquidity_index = (
            header.index(self._liquidity_field)
            if self._liquidity_field is not None and self._liquidity_field in header
            else None
        )

        out: list[dict[str, Any]] = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) <= index:
                continue
            try:
                when = pd.Timestamp(parts[0].strip(), tz=UTC)
            except (TypeError, ValueError):
                continue
            scalar = _finite(parts[index])
            if scalar is None:
                continue
            record = {
                "asset": symbol,
                "event_date": when,
                "knowledge_date": when,
                "value": scalar,
            }
            if liquidity_index is not None:
                record["liquidity"] = _finite(parts[liquidity_index])
            out.append(record)
        return out


def _epoch(raw: str) -> pd.Timestamp | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    # Binance switched some archive files to microseconds; the magnitude is the
    # only signal and a wrong unit lands the bar in 1970 or in the year 55000.
    if value > 1e14:
        value //= 1000
    try:
        return pd.Timestamp(value, unit="ms", tz=UTC)
    except (ValueError, OverflowError):
        return None


def _period_end(period: str) -> pd.Timestamp:
    stamp = pd.Timestamp(period, tz=UTC)
    return stamp + pd.offsets.MonthEnd(0) if len(period) == 7 else stamp


def _period_wanted(
    period: str, start: pd.Timestamp | None, end: pd.Timestamp | None
) -> bool:
    begins = pd.Timestamp(period, tz=UTC)
    finishes = _period_end(period)
    if start is not None and finishes < start.floor("D"):
        return False
    if end is not None and begins > end:
        return False
    return True
