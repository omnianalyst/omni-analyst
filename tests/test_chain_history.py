"""Historical on-chain flow: paged address history, truncation, resume.

The properties asserted here are the ones that decide whether a backfilled
exchange-reserve total means anything:

- **A window at Etherscan's 10,000-result cap is TRUNCATED, not complete.** The
  headline. At the cap the payload for "exactly 10,000 transfers" and "40,000
  transfers, of which here are the first 10,000" are byte-identical, so accepting
  it produces a flow total short by an unknown amount that looks perfectly
  healthy. Its counterpart (9,999 results returns normally) is asserted too --
  otherwise "always raise" would pass.
- Direction is the tracked address's own side of the transfer, using
  ``onchain.py``'s exact strings, because ``conviction/reserve.py`` and
  ``conviction/convergence_producer.py`` read those strings.
- ``event_date == knowledge_date``, asserted rather than assumed: a confirmed
  transaction is public the moment it is mined.
- Resume across two windows covers every block exactly once -- no gap, no
  re-emitted transaction -- with the cursor re-read from the database between
  them, which is what a restart at hour two of a 2.7-hour backfill actually is.
- A skip is local: one malformed transaction does not cost its neighbours.
- An error-shaped response raises ``Unavailable``, while Etherscan's
  error-shaped "No transactions found" is data (an empty range), not a failure.

No network: every payload is recorded shape, served through an injected
``fetch_fn``.
"""

import pytest

from omni.ingest.chain.cursor import get_cursor
from omni.ingest.chain.history import (
    HistoryReport,
    WindowTruncated,
    backfill_address,
    cursor_key,
    fetch_address_history,
    parse_address_history,
)
from omni.ingest.protocol import Unavailable

BINANCE_14 = "0x28c6c06298d514db089934071355e5743bf21d60"
COUNTERPARTY = "0x5f65f7b609678448494de4c87521cdf6cef1e932"
OTHER = "0x9b7f4a3c1e2d5068b71f4b9a3c8d2e6f0a1b2c3d"
CHAIN = "eth"
API_KEY = "test-key"

_BASE_TS = 1742000000
ONE_ETH = 10**18

# Etherscan's own documented ceiling on a single query, written out rather than
# imported: importing the module's constant would make these tests agree with
# whatever the module believes, and a module that believes the cap is 20,000
# would pass its own truncation test while returning half-empty windows.
ETHERSCAN_CAP = 10_000


def _tx(
    *,
    block: int,
    frm: str,
    to: str,
    value: int | str,
    tx_hash: str | None = None,
    ts: int | None = None,
    is_error: str = "0",
) -> dict:
    """One row of a real ``module=account&action=txlist`` response."""
    return {
        "blockNumber": str(block),
        "timeStamp": str(_BASE_TS + block if ts is None else ts),
        "hash": tx_hash or f"0x{block:064x}",
        "nonce": "1",
        "blockHash": f"0x{block:064x}",
        "transactionIndex": "3",
        "from": frm,
        "to": to,
        "value": str(value),
        "gas": "21000",
        "gasPrice": "12000000000",
        "isError": is_error,
        "txreceipt_status": "1",
        "input": "0x",
        "contractAddress": "",
        "cumulativeGasUsed": "1234567",
        "gasUsed": "21000",
        "confirmations": "104",
        "methodId": "0x",
        "functionName": "",
    }


def _bulk(n: int, *, first_block: int = 900000) -> list[dict]:
    return [
        _tx(
            block=first_block + i,
            frm=COUNTERPARTY,
            to=BINANCE_14,
            value=ONE_ETH,
            tx_hash=f"0x{i:064x}",
        )
        for i in range(n)
    ]


_OK = {"status": "1", "message": "OK"}
_NO_TX = {"status": "0", "message": "No transactions found", "result": []}


def _serve(rows: list[dict]):
    """A ``fetch_fn`` paging ``rows`` exactly as Etherscan does.

    An exhausted page answers with the real error-shaped "No transactions found"
    body rather than an empty OK, so the pagination path is exercised against
    the shape the live API returns.
    """
    calls: list[dict] = []

    async def fetch(params):
        calls.append(params)
        page, size = int(params["page"]), int(params["offset"])
        chunk = rows[(page - 1) * size : page * size]
        if not chunk:
            return dict(_NO_TX)
        return {**_OK, "result": chunk}

    fetch.calls = calls
    return fetch


def _serve_range(rows: list[dict]):
    """A ``fetch_fn`` that honours ``startblock``/``endblock``, then pages."""
    calls: list[dict] = []

    async def fetch(params):
        calls.append(params)
        lo, hi = int(params["startblock"]), int(params["endblock"])
        window = [r for r in rows if lo <= int(r["blockNumber"]) <= hi]
        page, size = int(params["page"]), int(params["offset"])
        chunk = window[(page - 1) * size : page * size]
        if not chunk:
            return dict(_NO_TX)
        return {**_OK, "result": chunk}

    fetch.calls = calls
    return fetch


class TestParseDirection:
    def test_page_of_transfers_produces_one_claim_each(self):
        txs = [
            _tx(block=910000 + i, frm=COUNTERPARTY, to=BINANCE_14, value=ONE_ETH * (i + 1))
            for i in range(4)
        ]
        drafts = parse_address_history(txs, address=BINANCE_14, chain=CHAIN)
        assert len(drafts) == 4
        assert [d.key for d in drafts] == [t["hash"] for t in txs]
        assert [d.value["amount_eth"] for d in drafts] == [1.0, 2.0, 3.0, 4.0]
        assert all(d.claim_type == "onchain_flow" for d in drafts)
        assert all(d.unit == "ETH" for d in drafts)
        assert [d.evidence["block"] for d in drafts] == [910000 + i for i in range(4)]

    def test_inbound_and_outbound_get_opposite_directions(self):
        txs = [
            _tx(block=910001, frm=COUNTERPARTY, to=BINANCE_14, value=5 * ONE_ETH),
            _tx(block=910002, frm=BINANCE_14, to=COUNTERPARTY, value=7 * ONE_ETH),
        ]
        inbound, outbound = parse_address_history(txs, address=BINANCE_14, chain=CHAIN)
        assert inbound.value["direction"] == "inflow"
        assert outbound.value["direction"] == "outflow"
        # The exact strings reserve.py and convergence_producer.py read. A
        # synonym here reads downstream as no flow at all.
        assert inbound.value["kind"] == "exchange_inflow"
        assert outbound.value["kind"] == "exchange_outflow"
        assert inbound.value["amount_eth"] == 5.0
        assert outbound.value["amount_eth"] == 7.0

    def test_direction_follows_the_queried_address_not_the_row_order(self):
        # The same two rows, parsed from the COUNTERPARTY's point of view, must
        # flip: direction is a property of whose history is being read.
        txs = [
            _tx(block=910001, frm=COUNTERPARTY, to=BINANCE_14, value=5 * ONE_ETH),
            _tx(block=910002, frm=BINANCE_14, to=COUNTERPARTY, value=7 * ONE_ETH),
        ]
        first, second = parse_address_history(txs, address=COUNTERPARTY, chain=CHAIN)
        assert first.value["direction"] == "outflow"
        assert second.value["direction"] == "inflow"

    def test_address_matching_is_case_insensitive(self):
        txs = [_tx(block=910001, frm=COUNTERPARTY, to=BINANCE_14.upper(), value=ONE_ETH)]
        (draft,) = parse_address_history(txs, address=BINANCE_14, chain=CHAIN)
        assert draft.value["direction"] == "inflow"
        assert draft.value["to"] == BINANCE_14

    def test_transfer_touching_neither_side_is_skipped(self):
        txs = [_tx(block=910001, frm=COUNTERPARTY, to=OTHER, value=ONE_ETH)]
        assert parse_address_history(txs, address=BINANCE_14, chain=CHAIN) == []

    def test_self_transfer_is_skipped(self):
        txs = [_tx(block=910001, frm=BINANCE_14, to=BINANCE_14, value=ONE_ETH)]
        assert parse_address_history(txs, address=BINANCE_14, chain=CHAIN) == []

    def test_exchange_label_defaults_to_the_known_address_set(self):
        txs = [_tx(block=910001, frm=COUNTERPARTY, to=BINANCE_14, value=ONE_ETH)]
        (draft,) = parse_address_history(txs, address=BINANCE_14, chain=CHAIN)
        assert draft.value["exchange"] == "Binance 14"

    def test_supplied_label_is_used_and_unknown_address_stays_unlabelled(self):
        txs = [_tx(block=910001, frm=COUNTERPARTY, to=OTHER, value=ONE_ETH)]
        (labelled,) = parse_address_history(
            txs, address=OTHER, chain=CHAIN, label="Kraken 9"
        )
        assert labelled.value["exchange"] == "Kraken 9"
        (unlabelled,) = parse_address_history(txs, address=OTHER, chain=CHAIN)
        assert unlabelled.value["exchange"] is None


class TestBitemporal:
    def test_event_date_equals_knowledge_date(self):
        txs = [
            _tx(block=910001, frm=COUNTERPARTY, to=BINANCE_14, value=ONE_ETH, ts=1742000101),
            _tx(block=910002, frm=BINANCE_14, to=COUNTERPARTY, value=ONE_ETH, ts=1742000199),
        ]
        drafts = parse_address_history(txs, address=BINANCE_14, chain=CHAIN)
        assert len(drafts) == 2
        for draft, expected_ts in zip(drafts, (1742000101, 1742000199), strict=True):
            assert draft.event_date == draft.knowledge_date
            assert int(draft.event_date.timestamp()) == expected_ts
            assert int(draft.knowledge_date.timestamp()) == expected_ts


class TestValueParsing:
    def test_zero_value_transaction_is_skipped_and_neighbours_survive(self):
        txs = [
            _tx(block=910001, frm=COUNTERPARTY, to=BINANCE_14, value=ONE_ETH),
            _tx(block=910002, frm=COUNTERPARTY, to=BINANCE_14, value=0),
            _tx(block=910003, frm=BINANCE_14, to=COUNTERPARTY, value=2 * ONE_ETH),
        ]
        drafts = parse_address_history(txs, address=BINANCE_14, chain=CHAIN)
        assert [d.evidence["block"] for d in drafts] == [910001, 910003]

    def test_dust_far_below_one_wei_of_eth_is_not_treated_as_zero(self):
        # 1 wei is 1e-18 ETH, which is not zero. The skip is an exact integer
        # test on wei, so a float comparison against 0.0 cannot swallow it.
        txs = [_tx(block=910001, frm=COUNTERPARTY, to=BINANCE_14, value=1)]
        (draft,) = parse_address_history(txs, address=BINANCE_14, chain=CHAIN)
        assert draft.value["amount_wei"] == "1"
        assert draft.value["amount_eth"] > 0.0

    def test_failed_transaction_is_skipped(self):
        txs = [
            _tx(block=910001, frm=COUNTERPARTY, to=BINANCE_14, value=ONE_ETH, is_error="1"),
            _tx(block=910002, frm=COUNTERPARTY, to=BINANCE_14, value=ONE_ETH),
        ]
        drafts = parse_address_history(txs, address=BINANCE_14, chain=CHAIN)
        assert [d.evidence["block"] for d in drafts] == [910002]

    def test_exact_wei_is_carried_because_the_eth_float_is_lossy(self):
        # 12345.678901234567890123 ETH: the wei integer exceeds 2**53, so the
        # ETH float cannot round-trip it and the raw integer must survive.
        wei = 12345678901234567890123
        txs = [_tx(block=910001, frm=COUNTERPARTY, to=BINANCE_14, value=wei)]
        (draft,) = parse_address_history(txs, address=BINANCE_14, chain=CHAIN)
        assert draft.value["amount_wei"] == str(wei)
        assert int(draft.value["amount_wei"]) == wei
        assert draft.value["amount_eth"] == pytest.approx(wei / 1e18)
        assert int(draft.value["amount_eth"] * 1e18) != wei

    def test_malformed_transactions_are_skipped_and_neighbours_emitted(self):
        good_before = _tx(block=910001, frm=COUNTERPARTY, to=BINANCE_14, value=ONE_ETH)
        no_timestamp = _tx(block=910002, frm=COUNTERPARTY, to=BINANCE_14, value=ONE_ETH)
        del no_timestamp["timeStamp"]
        bad_value = _tx(block=910003, frm=COUNTERPARTY, to=BINANCE_14, value="not-a-number")
        no_hash = _tx(block=910004, frm=COUNTERPARTY, to=BINANCE_14, value=ONE_ETH)
        no_hash["hash"] = ""
        good_after = _tx(block=910005, frm=BINANCE_14, to=COUNTERPARTY, value=3 * ONE_ETH)

        drafts = parse_address_history(
            [good_before, no_timestamp, bad_value, no_hash, good_after],
            address=BINANCE_14,
            chain=CHAIN,
        )
        assert [d.evidence["block"] for d in drafts] == [910001, 910005]
        assert [d.value["direction"] for d in drafts] == ["inflow", "outflow"]
        assert drafts[1].value["amount_eth"] == 3.0

    def test_fractional_value_is_refused_not_truncated(self):
        # A float on the wire is not the documented shape; int() would silently
        # truncate it into a real-looking wei count.
        txs = [_tx(block=910001, frm=COUNTERPARTY, to=BINANCE_14, value=ONE_ETH)]
        txs[0]["value"] = 1.5
        assert parse_address_history(txs, address=BINANCE_14, chain=CHAIN) == []


class TestFetchPagination:
    async def test_partial_page_ends_pagination(self):
        rows = _bulk(150)
        fetch = _serve(rows)
        out = await fetch_address_history(
            address=BINANCE_14,
            start_block=900000,
            end_block=900200,
            api_key=API_KEY,
            fetch_fn=fetch,
            page_size=100,
        )
        assert len(out) == 150
        # Page 2 returned 50 (< page_size), so there is no page 3.
        assert [int(c["page"]) for c in fetch.calls] == [1, 2]

    async def test_full_final_page_forces_one_more_call(self):
        rows = _bulk(200)
        fetch = _serve(rows)
        out = await fetch_address_history(
            address=BINANCE_14,
            start_block=900000,
            end_block=900300,
            api_key=API_KEY,
            fetch_fn=fetch,
            page_size=100,
        )
        assert len(out) == 200
        assert [int(c["page"]) for c in fetch.calls] == [1, 2, 3]

    async def test_call_shape_is_the_v2_txlist_query(self):
        fetch = _serve(_bulk(10))
        await fetch_address_history(
            address=BINANCE_14,
            start_block=900000,
            end_block=900055,
            api_key=API_KEY,
            fetch_fn=fetch,
            page_size=100,
        )
        (params,) = fetch.calls
        # chainid is V2's; without it the query silently answers for whichever
        # chain the API defaults to.
        assert params["chainid"] == 1
        assert params["module"] == "account"
        assert params["action"] == "txlist"
        assert params["address"] == BINANCE_14
        assert params["startblock"] == 900000
        assert params["endblock"] == 900055
        assert params["offset"] == 100
        assert params["sort"] == "asc"
        assert params["apikey"] == API_KEY

    async def test_empty_range_is_data_not_failure(self):
        async def fetch(params):
            return dict(_NO_TX)

        out = await fetch_address_history(
            address=BINANCE_14,
            start_block=900000,
            end_block=900055,
            api_key=API_KEY,
            fetch_fn=fetch,
        )
        assert out == []

    async def test_end_before_start_refused(self):
        with pytest.raises(ValueError):
            await fetch_address_history(
                address=BINANCE_14,
                start_block=900100,
                end_block=900000,
                api_key=API_KEY,
                fetch_fn=_serve([]),
            )


class TestTruncation:
    async def test_exactly_ten_thousand_results_is_truncated_not_complete(self):
        # THE headline. 100 full pages of 100 is Etherscan's hard cap. The
        # payload is identical whether the window held exactly 10,000 transfers
        # or 400,000, so the only safe reading is "truncated by an unknown
        # amount" -- returning the list would hand the reserve producer a total
        # that is short and looks healthy.
        fetch = _serve(_bulk(ETHERSCAN_CAP))
        with pytest.raises(WindowTruncated):
            await fetch_address_history(
                address=BINANCE_14,
                start_block=900000,
                end_block=905000,
                api_key=API_KEY,
                fetch_fn=fetch,
                page_size=100,
            )

    async def test_one_below_the_cap_returns_normally(self):
        # The counterpart that stops "always raise" from passing the test above.
        fetch = _serve(_bulk(ETHERSCAN_CAP - 1))
        out = await fetch_address_history(
            address=BINANCE_14,
            start_block=900000,
            end_block=905000,
            api_key=API_KEY,
            fetch_fn=fetch,
            page_size=100,
        )
        assert len(out) == ETHERSCAN_CAP - 1

    async def test_truncation_is_an_unavailable_so_the_pipeline_records_it(self):
        fetch = _serve(_bulk(ETHERSCAN_CAP))
        with pytest.raises(Unavailable):
            await fetch_address_history(
                address=BINANCE_14,
                start_block=900000,
                end_block=905000,
                api_key=API_KEY,
                fetch_fn=fetch,
                page_size=100,
            )

    async def test_cap_detected_in_a_single_large_page(self):
        fetch = _serve(_bulk(ETHERSCAN_CAP))
        with pytest.raises(WindowTruncated):
            await fetch_address_history(
                address=BINANCE_14,
                start_block=900000,
                end_block=905000,
                api_key=API_KEY,
                fetch_fn=fetch,
                page_size=ETHERSCAN_CAP,
            )


class TestErrorShapedResponses:
    async def test_error_shaped_response_raises_unavailable(self):
        async def fetch(params):
            return {"status": "0", "message": "NOTOK", "result": "Invalid API Key"}

        with pytest.raises(Unavailable) as exc:
            await fetch_address_history(
                address=BINANCE_14,
                start_block=900000,
                end_block=900055,
                api_key=API_KEY,
                fetch_fn=fetch,
            )
        assert "Invalid API Key" in str(exc.value)

    async def test_deprecated_v1_endpoint_response_raises(self):
        async def fetch(params):
            return {
                "status": "0",
                "message": "NOTOK",
                "result": "You are using a deprecated V1 endpoint",
            }

        with pytest.raises(Unavailable):
            await fetch_address_history(
                address=BINANCE_14,
                start_block=900000,
                end_block=900055,
                api_key=API_KEY,
                fetch_fn=fetch,
            )

    async def test_non_object_payload_raises(self):
        async def fetch(params):
            return ["not", "an", "object"]

        with pytest.raises(Unavailable):
            await fetch_address_history(
                address=BINANCE_14,
                start_block=900000,
                end_block=900055,
                api_key=API_KEY,
                fetch_fn=fetch,
            )

    async def test_ok_status_with_string_result_raises(self):
        async def fetch(params):
            return {"status": "1", "message": "OK", "result": "surprise"}

        with pytest.raises(Unavailable):
            await fetch_address_history(
                address=BINANCE_14,
                start_block=900000,
                end_block=900055,
                api_key=API_KEY,
                fetch_fn=fetch,
            )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE chain_cursor")
    yield


class TestBackfillWindow:
    async def test_first_window_ends_at_head_and_advances_the_cursor(self, db):
        txs = [
            _tx(block=b, frm=COUNTERPARTY, to=BINANCE_14, value=ONE_ETH)
            for b in range(991, 1001)
        ]
        report = await backfill_address(
            db.pool,
            address=BINANCE_14,
            chain=CHAIN,
            api_key=API_KEY,
            head_block=1000,
            max_span=10,
            fetch_fn=_serve_range(txs),
        )
        assert isinstance(report, HistoryReport)
        assert (report.start_block, report.end_block) == (991, 1000)
        assert report.transactions == 10
        assert len(report.drafts) == 10
        assert report.advanced is True
        assert report.halvings == 0
        assert await get_cursor(db.pool, cursor_key(CHAIN, BINANCE_14)) == 1000

    async def test_caught_up_reports_without_fetching(self, db):
        fetch = _serve_range([])
        await backfill_address(
            db.pool,
            address=BINANCE_14,
            chain=CHAIN,
            api_key=API_KEY,
            head_block=1000,
            max_span=10,
            fetch_fn=_serve_range(
                [_tx(block=1000, frm=COUNTERPARTY, to=BINANCE_14, value=ONE_ETH)]
            ),
        )
        report = await backfill_address(
            db.pool,
            address=BINANCE_14,
            chain=CHAIN,
            api_key=API_KEY,
            head_block=1000,
            max_span=10,
            fetch_fn=fetch,
        )
        assert report.caught_up is True
        assert report.advanced is False
        assert report.drafts == ()
        assert fetch.calls == [], "caught up must not spend an API call"

    async def test_each_address_keeps_its_own_cursor(self, db):
        txs = [
            _tx(block=b, frm=COUNTERPARTY, to=BINANCE_14, value=ONE_ETH)
            for b in range(991, 1001)
        ]
        await backfill_address(
            db.pool,
            address=BINANCE_14,
            chain=CHAIN,
            api_key=API_KEY,
            head_block=1000,
            max_span=10,
            fetch_fn=_serve_range(txs),
        )
        # A second address on the same chain has read nothing. Sharing one "eth"
        # cursor would mark its unread blocks as covered.
        assert await get_cursor(db.pool, cursor_key(CHAIN, OTHER)) is None
        report = await backfill_address(
            db.pool,
            address=OTHER,
            chain=CHAIN,
            api_key=API_KEY,
            head_block=1000,
            max_span=10,
            fetch_fn=_serve_range(
                [_tx(block=995, frm=OTHER, to=COUNTERPARTY, value=ONE_ETH)]
            ),
        )
        assert report.caught_up is False
        assert (report.start_block, report.end_block) == (991, 1000)


class TestResume:
    async def test_two_windows_resume_with_no_gap_and_no_duplicate(self, db):
        # One transfer per block across 991..1010. Window one runs to head 1000;
        # the cursor is then re-read from the database (what a restart at hour
        # two of a 2.7-hour backfill actually is) and window two continues.
        txs = [
            _tx(
                block=b,
                frm=COUNTERPARTY if b % 2 else BINANCE_14,
                to=BINANCE_14 if b % 2 else COUNTERPARTY,
                value=ONE_ETH,
            )
            for b in range(991, 1011)
        ]
        key = cursor_key(CHAIN, BINANCE_14)

        first = await backfill_address(
            db.pool,
            address=BINANCE_14,
            chain=CHAIN,
            api_key=API_KEY,
            head_block=1000,
            max_span=10,
            fetch_fn=_serve_range(txs),
        )
        assert (first.start_block, first.end_block) == (991, 1000)

        # --- restart: the only surviving state is the cursor row ---
        resumed_from = await get_cursor(db.pool, key)
        assert resumed_from == 1000

        second = await backfill_address(
            db.pool,
            address=BINANCE_14,
            chain=CHAIN,
            api_key=API_KEY,
            head_block=1010,
            max_span=10,
            fetch_fn=_serve_range(txs),
        )
        assert second.start_block == first.end_block + 1, "no gap"
        assert second.start_block == resumed_from + 1
        assert second.end_block == 1010

        first_hashes = [d.key for d in first.drafts]
        second_hashes = [d.key for d in second.drafts]
        assert set(first_hashes).isdisjoint(second_hashes), "re-emitted a transfer"
        assert len(first_hashes) == 10
        assert len(second_hashes) == 10

        blocks = [d.evidence["block"] for d in first.drafts + second.drafts]
        assert sorted(blocks) == list(range(991, 1011)), "gap or duplicate in coverage"
        assert await get_cursor(db.pool, key) == 1010
        # Both directions survived the round trip, keyed off each row's own side.
        assert {d.value["direction"] for d in first.drafts} == {"inflow", "outflow"}

    async def test_empty_window_with_a_prior_cursor_still_advances(self, db):
        key = cursor_key(CHAIN, BINANCE_14)
        await backfill_address(
            db.pool,
            address=BINANCE_14,
            chain=CHAIN,
            api_key=API_KEY,
            head_block=1000,
            max_span=10,
            fetch_fn=_serve_range(
                [_tx(block=1000, frm=COUNTERPARTY, to=BINANCE_14, value=ONE_ETH)]
            ),
        )
        before = await db.pool.fetchrow(
            "SELECT last_block, last_block_at FROM chain_cursor WHERE chain = $1", key
        )
        report = await backfill_address(
            db.pool,
            address=BINANCE_14,
            chain=CHAIN,
            api_key=API_KEY,
            head_block=1010,
            max_span=10,
            fetch_fn=_serve_range([]),
        )
        after = await db.pool.fetchrow(
            "SELECT last_block, last_block_at FROM chain_cursor WHERE chain = $1", key
        )
        assert report.advanced is True
        assert report.drafts == ()
        assert after["last_block"] == 1010
        # No transaction was observed, so no new block time was learned. The
        # stored time is the last one actually seen, never now().
        assert after["last_block_at"] == before["last_block_at"]

    async def test_empty_first_window_refuses_to_invent_a_block_time(self, db):
        report = await backfill_address(
            db.pool,
            address=BINANCE_14,
            chain=CHAIN,
            api_key=API_KEY,
            head_block=1000,
            max_span=10,
            fetch_fn=_serve_range([]),
        )
        assert report.advanced is False
        assert report.transactions == 0
        assert await get_cursor(db.pool, cursor_key(CHAIN, BINANCE_14)) is None


class TestBackfillTruncation:
    def _capped_above(self, span_limit: int, small: list[dict]):
        """Serve the 10,000 cap for any window wider than ``span_limit``."""
        cap_rows = _bulk(ETHERSCAN_CAP)
        calls: list[dict] = []

        async def fetch(params):
            calls.append(params)
            lo, hi = int(params["startblock"]), int(params["endblock"])
            page, size = int(params["page"]), int(params["offset"])
            source = cap_rows if (hi - lo + 1) > span_limit else [
                r for r in small if lo <= int(r["blockNumber"]) <= hi
            ]
            chunk = source[(page - 1) * size : page * size]
            if not chunk:
                return dict(_NO_TX)
            return {**_OK, "result": chunk}

        fetch.calls = calls
        return fetch

    async def test_truncated_window_is_halved_and_retried(self, db):
        small = [
            _tx(block=b, frm=COUNTERPARTY, to=BINANCE_14, value=ONE_ETH)
            for b in range(991, 996)
        ]
        report = await backfill_address(
            db.pool,
            address=BINANCE_14,
            chain=CHAIN,
            api_key=API_KEY,
            head_block=1000,
            max_span=10,
            fetch_fn=self._capped_above(5, small),
        )
        assert report.halvings == 1
        assert (report.start_block, report.end_block) == (991, 995)
        assert report.requested_end_block == 1000
        assert len(report.drafts) == 5
        # The cursor stops at the end of the window actually read WHOLE, so the
        # blocks the halving gave up are read by the next call, not skipped.
        assert await get_cursor(db.pool, cursor_key(CHAIN, BINANCE_14)) == 995

        follow_up = await backfill_address(
            db.pool,
            address=BINANCE_14,
            chain=CHAIN,
            api_key=API_KEY,
            head_block=1000,
            max_span=10,
            fetch_fn=_serve_range(
                [
                    _tx(block=b, frm=BINANCE_14, to=COUNTERPARTY, value=ONE_ETH)
                    for b in range(996, 1001)
                ]
            ),
        )
        assert (follow_up.start_block, follow_up.end_block) == (996, 1000)

    async def test_single_block_that_stays_truncated_raises(self, db):
        fetch = self._capped_above(0, [])
        with pytest.raises(WindowTruncated):
            await backfill_address(
                db.pool,
                address=BINANCE_14,
                chain=CHAIN,
                api_key=API_KEY,
                head_block=1000,
                max_span=1,
                fetch_fn=fetch,
            )
        # A window that could not be read whole leaves the cursor untouched, so
        # nothing downstream believes those blocks are covered.
        assert await get_cursor(db.pool, cursor_key(CHAIN, BINANCE_14)) is None

    async def test_truncation_never_yields_a_short_window_as_complete(self, db):
        # The whole point, stated at the backfill level: a capped window must not
        # produce 10,000 drafts and a cursor advanced past blocks nobody read.
        fetch = self._capped_above(0, [])
        with pytest.raises(Unavailable):
            await backfill_address(
                db.pool,
                address=BINANCE_14,
                chain=CHAIN,
                api_key=API_KEY,
                head_block=1000,
                max_span=1,
                fetch_fn=fetch,
            )
