"""Paper trader: scan live markets, log would-trades, resolve, report.

Three operating modes, all driven off a single JSONL log file:

- **scan**: fetch active Yes/No markets, run the anchored LLM elicitation on
  each, log a `trade_open` event for every market where
  `|llm_prob - yes_price| >= threshold`. Idempotent on (market_id, day): a
  market already logged today is not re-logged, so calling scan daily does
  not duplicate positions.

- **resolve**: for every `trade_open` without a matching `trade_close`,
  fetch the market's current state from Gamma. If resolved, compute P&L
  using `pnl.Fill` and append a `trade_close` event.

- **report**: read the log, summarise closed trades (gross/fees/net/win
  rate/avg ROI/drawdown) and list open trades.

**No live orders, no real money, no `venue/` touch.** This is a forward
backtest — it tests whether the Stage A edge survives real costs on real
markets resolved in real time, without risking capital. The runner prints
the paper P&L; a parallel $100 live allocation would confirm whether the
simulated fills match reality.

The log is JSONL, append-only, line-delimited. One record per event. The
schema is small and documented inline; if a record cannot be parsed (a
corrupted line, an unknown event type) the report says so and skips, rather
than aborting — a paper-trader that aborts on one bad line loses the whole
run's data.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from omni.ingest.protocol import Unavailable
from omni.llm.protocol import LanguageModel
from omni.polymarket.active import ActiveMarket, fetch_current_resolution, list_active_markets
from omni.polymarket.news import derive_category
from omni.polymarket.pnl import DEFAULT_FEE_RATES, Fill, summarise
from omni.polymarket.types import Document, Estimation, MarketAtCutoff, ResolvedMarket

DEFAULT_THRESHOLD = 0.05  # 5% absolute edge required to open a paper trade
DEFAULT_SIZE_USD = 5.0
SCAN_IDEMPOTENCY_WINDOW_HOURS = 20  # ~daily; re-scanning within 20h is a no-op


@dataclass(frozen=True)
class TradeOpen:
    type: str = "trade_open"
    id: str = ""
    ts: str = ""
    market_id: str = ""
    question: str = ""
    category: str = ""
    direction: str = ""        # "YES" or "NO"
    entry_price: float = 0.0   # share price at scan time
    size_shares: float = 0.0
    size_usd: float = 0.0
    llm_prob: float = 0.0      # model's P(yes)
    market_prob: float = 0.0   # yes_price at scan
    fee_rate: float = 0.0
    taker: bool = False
    method: str = ""
    model: str = ""


@dataclass(frozen=True)
class TradeClose:
    type: str = "trade_close"
    trade_id: str = ""
    ts: str = ""
    market_id: str = ""
    outcome_yes: bool = False
    final_yes_price: float = 0.0
    gross_pnl: float = 0.0
    fee_pnl: float = 0.0
    net_pnl: float = 0.0
    roi_pct: float = 0.0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _read_log(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _append_log(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _active_market_to_resolved_shape(market: ActiveMarket) -> ResolvedMarket:
    """ActiveMarket -> ResolvedMarket shape, for the LLM snapshot.

    The LLM elicitation operates on `MarketAtCutoff`, which carries a
    `ResolvedMarket`. For a live market we do not have a resolution yet;
    we use the market's end_date as a stand-in `resolution_date` and a
    placeholder `resolved_yes=False`. The placeholder is never read by the
    elicitation — only by the resolver, which does not run on live markets.
    """
    resolution_date = market.end_date or (datetime.now(UTC) + timedelta(days=30))
    return ResolvedMarket(
        condition_id=market.condition_id,
        question=market.question,
        category=market.category,
        resolved_yes=False,
        resolution_date=resolution_date,
        created_at=datetime.now(UTC) - timedelta(days=1),
        yes_token_id=market.yes_token_id,
        no_token_id=market.no_token_id,
        neg_risk=market.neg_risk,
        slug=market.slug,
        volume=market.volume,
    )


def _active_market_to_snapshot(
    market: ActiveMarket,
    *,
    documents: tuple[Document, ...],
    cutoff_horizon_days: int = 7,
) -> MarketAtCutoff:
    """Build a MarketAtCutoff for the LLM. The cutoff is "now" for live
    markets — we are estimating from current information."""
    resolved_shape = _active_market_to_resolved_shape(market)
    cutoff = datetime.now(UTC)
    if resolved_shape.resolution_date <= cutoff:
        cutoff = resolved_shape.resolution_date - timedelta(days=cutoff_horizon_days)
        if cutoff <= resolved_shape.created_at:
            cutoff = resolved_shape.created_at + (resolved_shape.resolution_date - resolved_shape.created_at) / 2
    return MarketAtCutoff(
        market=resolved_shape,
        cutoff=cutoff,
        market_probability=market.yes_price,
        documents=documents,
    )


def _estimation_to_position(
    est: Estimation,
    market: ActiveMarket,
    *,
    threshold: float,
    size_usd: float,
    method: str,
    model_name: str,
) -> TradeOpen | None:
    """Convert an LLM estimate into a paper position, or None if below
    threshold. Returns None rather than a zero-size position so the scan
    can simply skip None entries.

    Direction mapping: estimator returns ("up"/"down", confidence). "up" =>
    trade YES; "down" => trade NO. For a NO trade the share price is (1 - p)
    and the share size is notional / (1 - p).
    """
    llm_p_yes = est.confidence if est.direction == "up" else 1.0 - est.confidence
    edge = abs(llm_p_yes - market.yes_price)
    if edge < threshold:
        return None

    direction = "YES" if est.direction == "up" else "NO"
    entry_price = market.yes_price if direction == "YES" else (1.0 - market.yes_price)
    if entry_price <= 0.0 or entry_price >= 1.0:
        # Edge case: market at 0 or 1 — no tradeable side.
        return None
    size_shares = size_usd / entry_price
    fee_rate = DEFAULT_FEE_RATES.get(derive_category(_active_market_to_resolved_shape(market)), 0.05)

    return TradeOpen(
        id=uuid.uuid4().hex,
        ts=_now_iso(),
        market_id=market.condition_id,
        question=market.question,
        category=market.category,
        direction=direction,
        entry_price=entry_price,
        size_shares=size_shares,
        size_usd=size_usd,
        llm_prob=llm_p_yes,
        market_prob=market.yes_price,
        fee_rate=fee_rate,
        taker=False,
        method=method,
        model=model_name,
    )


def _is_recently_logged(
    market_id: str,
    log: Sequence[dict],
    *,
    window_hours: float,
    now: datetime,
) -> bool:
    """Idempotency check: skip a market if a trade_open for it exists in the
    last `window_hours`. Without this, a daily scan would stack multiple
    positions on the same market as its price drifts."""
    cutoff = now - timedelta(hours=window_hours)
    for rec in log:
        if rec.get("type") != "trade_open":
            continue
        if rec.get("market_id") != market_id:
            continue
        try:
            ts = datetime.fromisoformat(rec["ts"])
        except (KeyError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts >= cutoff:
            return True
    return False


async def scan(
    client: httpx.AsyncClient,
    model: LanguageModel,
    log_path: Path,
    *,
    target_markets: int = 50,
    threshold: float = DEFAULT_THRESHOLD,
    size_usd: float = DEFAULT_SIZE_USD,
    method: str = "polymarket_paper_glm_5_2",
    min_volume: float = 1000.0,
    document_provider=None,
) -> tuple[int, int]:
    """One scan pass. Returns `(n_markets_seen, n_trades_opened)`.

    The scan is the main loop of the paper trader. A scheduler calls it
    daily; the JSONL log accumulates positions over time. The runner prints
    progress to stderr; the return value lets tests assert without parsing
    log output.
    """
    from omni.polymarket.estimator import estimate

    existing_log = _read_log(log_path)
    now = datetime.now(UTC)

    markets = await list_active_markets_until(
        client,
        target_count=target_markets,
        min_volume=min_volume,
    )

    n_opened = 0
    for market in markets:
        if _is_recently_logged(market.condition_id, existing_log, window_hours=SCAN_IDEMPOTENCY_WINDOW_HOURS, now=now):
            continue

        docs: tuple[Document, ...] = ()
        if document_provider is not None:
            try:
                docs = await document_provider(client, _active_market_to_resolved_shape(market), now)
            except Unavailable:
                docs = ()

        snap = _active_market_to_snapshot(market, documents=docs)
        try:
            est = await estimate(model, snap)
        except Unavailable:
            continue

        position = _estimation_to_position(
            est, market,
            threshold=threshold, size_usd=size_usd,
            method=method, model_name=model.name,
        )
        if position is None:
            continue
        _append_log(log_path, asdict(position))
        n_opened += 1

    return (len(markets), n_opened)


async def resolve(
    client: httpx.AsyncClient,
    log_path: Path,
) -> tuple[int, int]:
    """Resolve open paper positions. Returns `(n_resolved, n_still_open)`."""
    log = _read_log(log_path)
    opens = [r for r in log if r.get("type") == "trade_open"]
    closed_ids = {r.get("trade_id") for r in log if r.get("type") == "trade_close"}
    pending = [r for r in opens if r.get("id") not in closed_ids]

    n_resolved = 0
    n_still_open = 0
    for open_rec in pending:
        market_id = open_rec["market_id"]
        try:
            resolved_yes, final_price = await fetch_current_resolution(
                client, condition_id=market_id,
            )
        except Unavailable:
            n_still_open += 1
            continue

        if resolved_yes is None:
            n_still_open += 1
            continue

        fill = Fill(
            direction=open_rec["direction"],
            entry_price=open_rec["entry_price"],
            size_shares=open_rec["size_shares"],
            outcome_yes=resolved_yes,
            fee_rate=open_rec.get("fee_rate", 0.05),
            taker=open_rec.get("taker", False),
        )
        close = TradeClose(
            trade_id=open_rec["id"],
            ts=_now_iso(),
            market_id=market_id,
            outcome_yes=resolved_yes,
            final_yes_price=final_price if final_price is not None else (1.0 if resolved_yes else 0.0),
            gross_pnl=fill.gross_pnl,
            fee_pnl=fill.fee_pnl,
            net_pnl=fill.net_pnl,
            roi_pct=fill.roi_pct,
        )
        _append_log(log_path, asdict(close))
        n_resolved += 1

    return (n_resolved, n_still_open)


def report(log_path: Path) -> dict:
    """Read the log and return a summary dict.

    Returns `{"closed": PnLSummary.as_dict(), "open": [...], "log_path": str}`.
    Closed fills are summarised via `pnl.summarise`; open positions are
    listed as raw records for the caller to format.
    """
    log = _read_log(log_path)
    opens = [r for r in log if r.get("type") == "trade_open"]
    closes = [r for r in log if r.get("type") == "trade_close"]
    closed_ids = {r["trade_id"] for r in closes}
    open_positions = [r for r in opens if r.get("id") not in closed_ids]

    open_by_id = {r["id"]: r for r in opens}
    fills: list[Fill] = []
    for close_rec in closes:
        open_rec = open_by_id.get(close_rec["trade_id"])
        if open_rec is None:
            continue
        fills.append(
            Fill(
                direction=open_rec["direction"],
                entry_price=open_rec["entry_price"],
                size_shares=open_rec["size_shares"],
                outcome_yes=close_rec["outcome_yes"],
                fee_rate=open_rec.get("fee_rate", 0.05),
                taker=open_rec.get("taker", False),
            )
        )

    summary = summarise(fills)
    return {
        "n_open": len(open_positions),
        "n_closed": summary.n_closed,
        "n_wins": summary.n_wins,
        "win_rate": summary.win_rate,
        "gross_pnl": summary.gross_pnl,
        "fee_pnl": summary.fee_pnl,
        "net_pnl": summary.net_pnl,
        "avg_roi_pct": summary.avg_roi_pct,
        "worst_drawdown_usd": summary.worst_drawdown_usd,
        "open_positions": open_positions,
        "log_path": str(log_path),
    }


async def list_active_markets_until(
    client: httpx.AsyncClient,
    *,
    target_count: int,
    page_size: int = 100,
    max_pages: int = 5,
    min_volume: float = 1000.0,
    on_skip=None,
    on_page=None,
) -> list[ActiveMarket]:
    """Multi-page active-market fetch. Mirror of `gamma.list_resolved_markets_until`."""

    if target_count <= 0:
        raise ValueError(f"target_count must be positive: {target_count}")
    if page_size <= 0 or page_size > 500:
        raise ValueError(f"page_size must be in 1..500: {page_size}")
    if max_pages <= 0:
        raise ValueError(f"max_pages must be positive: {max_pages}")

    collected: list[ActiveMarket] = []
    for page in range(max_pages):
        if len(collected) >= target_count:
            break
        batch = await list_active_markets(
            client,
            limit=page_size,
            offset=page * page_size,
            min_volume=min_volume,
            strict=False,
            on_skip=on_skip,
        )
        if on_page is not None:
            on_page(page, len(batch))
        if not batch:
            break
        collected.extend(batch)
    return collected[:target_count]


__all__ = [
    "DEFAULT_SIZE_USD",
    "DEFAULT_THRESHOLD",
    "TradeClose",
    "TradeOpen",
    "report",
    "resolve",
    "scan",
]
