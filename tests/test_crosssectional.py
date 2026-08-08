"""The cross-sectional carry selector: which names the delta-neutral book holds.

The selector is pure and its tests use no database at all -- that separation is
the point of the module's shape, because the economics live in the ranking and
the hysteresis rather than in any query. The reader's tests use the real
visibility rule, the real claim table and the real audience scoping, because
those are exactly the three places a producer silently reads nothing.

Every test states the bug it catches. The load-bearing one is
`test_a_single_rank_selector_churns_the_whole_basket`: collapsing the exit rank
onto the enter rank is not a tuning choice, it is the difference between the
+7.35% net strategy measured in Finding 9 and the -20.5% one, and it is
invisible in the gross number.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from omni.conviction.crosssectional import (
    ABSTAIN_NO_COVERAGE,
    ABSTAIN_NON_FINITE_SCORE,
    ABSTAIN_UNIVERSE_TOO_SMALL,
    minimum_universe,
    select_basket,
    select_carry_basket,
)

NOW = datetime(2026, 3, 1, tzinfo=UTC)

# Eight hours, the real settlement cadence. A literal rather than the module's
# constant: the interval is a fact about the venue, and reading it from the code
# under test would let a mutation to that code mutate the fixture with it.
SETTLEMENT = timedelta(hours=8)


def _scores(**by_name: str) -> dict[str, Decimal]:
    """Scores keyed by readable names, so a ranking assertion reads as one."""
    return {name: Decimal(rate) for name, rate in by_name.items()}


# --- the pure selector: no database, no clock ------------------------------


class TestRankingDirection:
    def test_the_basket_is_the_names_paying_the_most(self):
        """Descending is the direction that pays.

        A short perp receives on a POSITIVE funding rate, so the book wants the
        highest trailing rates. The same measurement that put the top quartile
        at +8.74% put the bottom at -2.15%: an inverted sort is not a weaker
        strategy, it is the losing side of the same one, and it would still
        report a plausible-looking gross number.
        """
        scores = _scores(rich="0.0004", mid="0.0002", poor="0.0001", worst="-0.0003")

        result = select_basket(scores, held=(), enter_rank=1, exit_rank=2)

        assert result.abstention is None
        assert result.held == frozenset({"rich"})
        assert "worst" not in result.held

    def test_a_negative_rate_ranks_below_a_positive_one(self):
        # Sign, not magnitude: sorting on abs() would put the largest payer of
        # funding alongside the largest receiver of it.
        scores = _scores(pays="-0.0009", receives="0.0001", flat="0.0000")

        result = select_basket(scores, held=(), enter_rank=1, exit_rank=2)

        assert result.held == frozenset({"receives"})


class TestHysteresis:
    def test_a_single_rank_selector_churns_the_whole_basket(self):
        """The defect that turns +7.35% into -20.5%, and the reason the two
        ranks are separate parameters.

        A name sitting just outside the entry rank but inside the exit band is
        HELD, not sold. Under a single rank it is sold and rebought as the
        ranking jitters, and turnover is the one thing measured to destroy this
        strategy -- 29.19% of cost against 8.74% of gross at the fastest
        cadence.

        Asserted on the same inputs at two exit ranks so neither a
        never-releases selector nor a never-holds one satisfies both.
        """
        scores = _scores(a="0.0005", b="0.0004", c="0.0003", d="0.0002", e="0.0001", f="0.0000")
        held = frozenset({"c"})

        with_hysteresis = select_basket(scores, held=held, enter_rank=2, exit_rank=4)
        without = select_basket(scores, held=held, enter_rank=2, exit_rank=2)

        # c ranks third: outside the top 2, inside the top 4. It keeps its slot,
        # and the basket stays at enter_rank names -- so it displaces the
        # lower-ranked of the two entrants rather than being added alongside.
        assert "c" in with_hysteresis.held
        assert with_hysteresis.held == frozenset({"a", "c"})
        # Under a single rank the same name is ejected at the rank that would
        # buy it straight back.
        assert "c" not in without.held
        assert without.held == frozenset({"a", "b"})

    def test_a_name_falling_outside_the_exit_band_is_released(self):
        # The other half: hysteresis must not become buy-and-hold. A held name
        # that has genuinely stopped paying has to leave, or the basket only
        # ever grows.
        scores = _scores(a="0.0005", b="0.0004", c="0.0003", d="0.0002", e="0.0001", f="-0.0009")
        held = frozenset({"f"})

        result = select_basket(scores, held=held, enter_rank=2, exit_rank=4)

        assert "f" not in result.held
        assert result.held == frozenset({"a", "b"})

    def test_the_basket_never_exceeds_the_entry_rank(self):
        """The cap, and the 0.64pp it is worth.

        Without it the basket is `top(enter)` unioned with everything retained
        inside `top(exit)`, so it floats up toward `exit_rank` -- measured at
        13.6 names against an enter rank of 7. Half the capital then sits in
        names ranked below the entry cut, which by construction pay less, and
        Finding 11 measured that dilution at +7.16% net against +7.80%.

        It also makes per-name capital knowable before the selection runs, which
        a floating basket cannot: a position whose size is not known in advance
        cannot be risk-limited in advance.

        Every held name ranks inside the wide exit band, so a floating selector
        would keep all six and return six.
        """
        scores = _scores(
            a="0.0009", b="0.0008", c="0.0007", d="0.0006",
            e="0.0005", f="0.0004", g="0.0003", h="0.0002",
        )
        held = frozenset({"c", "d", "e", "f"})

        result = select_basket(scores, held=held, enter_rank=3, exit_rank=7)

        assert len(result.held) == 3
        # Retained names keep their slots in rank order and fill the basket, so
        # no entrant gets in at all here -- that is the churn being prevented.
        assert result.held == frozenset({"c", "d", "e"})

    def test_room_left_by_departures_goes_to_the_best_available(self):
        # The other half of the cap: it must not become buy-and-hold. When a
        # retained name leaves the exit band its slot is refilled from the top
        # of the ranking, not from whatever happened to be adjacent.
        scores = _scores(
            a="0.0009", b="0.0008", c="0.0007", d="0.0006",
            e="0.0005", f="0.0004", g="0.0003", h="-0.0090",
        )
        held = frozenset({"h", "d"})

        result = select_basket(scores, held=held, enter_rank=3, exit_rank=5)

        assert "h" not in result.held
        assert len(result.held) == 3
        assert result.held == frozenset({"d", "a", "b"})

    def test_an_exit_rank_tighter_than_entry_is_refused(self):
        # Not an abstention: it is a configuration that cannot be right. The
        # name would be ejected at the very rank that re-enters it, which is
        # worse churn than no hysteresis at all.
        with pytest.raises(ValueError, match="tighter than"):
            select_basket(_scores(a="0.1", b="0.2"), held=(), enter_rank=3, exit_rank=2)


class TestTheUniverseFloor:
    def test_a_rank_over_too_few_names_abstains(self):
        """"Top five of six" is inclusion, not a statement about relative
        funding. The floor keeps the selector from dressing up buy-everything.
        """
        scores = _scores(a="0.0005", b="0.0004", c="0.0003")

        result = select_basket(scores, held=(), enter_rank=2, exit_rank=4)

        assert result.abstention == ABSTAIN_UNIVERSE_TOO_SMALL
        assert result.held == frozenset()

    def test_an_abstention_holds_the_book_still_rather_than_liquidating(self):
        """Not rebalancing is the honest null action for a carry book.

        Liquidating because coverage thinned would be a trade the signal never
        asked for, and it would pay the turnover cost that the whole strategy is
        built to avoid.
        """
        held = frozenset({"a", "b"})

        result = select_basket(_scores(a="0.0005"), held=held, enter_rank=2, exit_rank=4)

        assert result.abstention == ABSTAIN_UNIVERSE_TOO_SMALL
        assert result.held == held

    def test_the_floor_requires_a_name_that_can_be_out(self):
        # With no more names than the exit rank, every name sits inside the exit
        # band permanently and hysteresis never releases anything.
        assert minimum_universe(enter_rank=2, exit_rank=6) == 7
        assert minimum_universe(enter_rank=5, exit_rank=5) == 10


class TestNonFiniteScores:
    def test_a_nan_rate_abstains_rather_than_being_dropped(self):
        """Every comparison against NaN is false, so a silently discarded name
        would shrink the universe and shift every rank below it without a trace.
        The abstention makes the bad datum visible instead.
        """
        scores = {
            "a": Decimal("0.0005"),
            "b": Decimal("0.0004"),
            "c": Decimal("NaN"),
            "d": Decimal("0.0002"),
            "e": Decimal("0.0001"),
            "f": Decimal("0.0000"),
        }

        result = select_basket(scores, held=(), enter_rank=2, exit_rank=4)

        assert result.abstention == ABSTAIN_NON_FINITE_SCORE

    def test_a_float_score_is_refused(self):
        # Funding rates are exact decimal strings in the store. A float score is
        # a lossy one, and the loss compounds over a six-week hold.
        with pytest.raises(TypeError, match="must be a Decimal"):
            select_basket({"a": 0.0005, "b": Decimal("0.1")}, held=(), enter_rank=1, exit_rank=2)


class TestDeterminism:
    def test_names_tied_on_funding_break_the_same_way_every_run(self):
        """Two names on identical trailing funding are indistinguishable to the
        signal. With no explicit total order Python's stable sort falls back on
        mapping insertion order, so the same inputs would give different baskets
        on different runs and the backtest would stop being reproducible.
        """
        tied = "0.0003"
        forward = {
            "aaa": Decimal(tied), "bbb": Decimal(tied), "ccc": Decimal(tied),
            "ddd": Decimal("0.0009"), "eee": Decimal("0.0008"), "fff": Decimal("0.0001"),
        }
        reversed_insertion = dict(reversed(list(forward.items())))

        a = select_basket(forward, held=(), enter_rank=3, exit_rank=5)
        b = select_basket(reversed_insertion, held=(), enter_rank=3, exit_rank=5)

        assert a.held == b.held
        # And it is the documented rule -- first by key among the tied names --
        # rather than merely stable.
        assert a.held == frozenset({"ddd", "eee", "aaa"})


# --- the reader: real claims, real visibility, real audience ---------------


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


async def _entity(db, symbol: str) -> UUID:
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('crypto_asset',$1,$1) "
        "RETURNING id",
        symbol,
    )


async def _funding(db, entity_id, rate, event_date, *, audience=None, knowledge=None):
    """One settlement. `knowledge_date` defaults to the settlement instant, which
    is what the real ingest writes: a rate is knowable when it settles."""
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, "
        "event_date, knowledge_date, confidence, redistributable, audience_user_id) "
        "VALUES ($1,'funding_rate','binance:TEST',$2::jsonb,'derivatives',$3,$4,1.0,"
        "$5,$6)",
        entity_id,
        json.dumps({"rate": str(rate), "venue": "binance", "symbol": "TEST"}),
        event_date,
        knowledge or event_date,
        "byo_only" if audience else "allowed",
        audience,
    )


async def _seed(db, symbol, rates, *, audience=None, end_at=NOW):
    """A name with one settlement per 8h ending just before `end_at`."""
    entity_id = await _entity(db, symbol)
    for i, rate in enumerate(rates):
        when = end_at - SETTLEMENT * (len(rates) - i)
        await _funding(db, entity_id, rate, when, audience=audience)
    return entity_id


async def _universe(db, spec, *, audience=None):
    return {sym: await _seed(db, sym, rates, audience=audience) for sym, rates in spec.items()}


class TestTheReaderIsPointInTime:
    async def test_a_settlement_filed_after_the_cutoff_is_invisible(self, db):
        """The defect that makes every backtest number a fiction.

        A rate knowable only tomorrow must not reach a decision made today. The
        planted settlement is enormous and would dominate the ranking if seen,
        so the assertion is on the resulting BASKET rather than on a row count:
        a filter that runs but reads the wrong column would still pass a count.
        """
        ids = await _universe(db, {
            "AAA": ["0.0001", "0.0001"],
            "BBB": ["0.0002", "0.0002"],
            "CCC": ["0.0003", "0.0003"],
            "DDD": ["0.0004", "0.0004"],
            "EEE": ["0.0005", "0.0005"],
            "FFF": ["0.0006", "0.0006"],
        })
        # AAA is the worst payer, but a huge rate becomes knowable tomorrow.
        await _funding(
            db, ids["AAA"], "9.9999",
            NOW - SETTLEMENT, knowledge=NOW + timedelta(days=1),
        )

        decision = await select_carry_basket(
            db.pool,
            entity_ids=list(ids.values()),
            audience_user_id=None,
            as_of=NOW,
            held=(),
            enter_rank=2,
            exit_rank=4,
        )

        assert decision.abstention is None
        assert ids["AAA"] not in decision.held
        assert decision.held == frozenset({ids["FFF"], ids["EEE"]})

    async def test_a_settlement_older_than_the_window_does_not_score(self, db):
        # The window is what makes the score TRAILING. Without the lower bound a
        # name's ancient regime would outvote its current one.
        ids = await _universe(db, {
            "AAA": ["0.0001", "0.0001"], "BBB": ["0.0002", "0.0002"],
            "CCC": ["0.0003", "0.0003"], "DDD": ["0.0004", "0.0004"],
            "EEE": ["0.0005", "0.0005"], "FFF": ["0.0006", "0.0006"],
        })
        await _funding(db, ids["AAA"], "9.9999", NOW - timedelta(days=90))

        decision = await select_carry_basket(
            db.pool,
            entity_ids=list(ids.values()),
            audience_user_id=None,
            as_of=NOW,
            held=(),
            enter_rank=2,
            exit_rank=4,
            lookback_days=7,
        )

        assert ids["AAA"] not in decision.held


class TestTheReaderRespectsTheAudience:
    async def test_another_audiences_rates_are_not_ranked(self, db):
        """Funding is byo_only. A rate one operator licensed must not open
        another's position, and the failure mode is silent: the reader returns
        fewer names and the rank quietly means something else.
        """
        mine = uuid4()
        theirs = uuid4()
        ids = await _universe(db, {
            "AAA": ["0.0001", "0.0001"], "BBB": ["0.0002", "0.0002"],
            "CCC": ["0.0003", "0.0003"], "DDD": ["0.0004", "0.0004"],
            "EEE": ["0.0005", "0.0005"], "FFF": ["0.0006", "0.0006"],
        }, audience=mine)
        # A name only the other operator can see, paying more than anything.
        other = await _seed(db, "ZZZ", ["9.9999", "9.9999"], audience=theirs)

        decision = await select_carry_basket(
            db.pool,
            entity_ids=[*ids.values(), other],
            audience_user_id=mine,
            as_of=NOW,
            held=(),
            enter_rank=2,
            exit_rank=4,
        )

        assert other not in decision.held
        assert other not in decision.scores

    async def test_no_visible_coverage_abstains_and_holds_the_book_still(self, db):
        # The exact shape of "the reader was not allowed to look", which cost
        # real time on this project reading as "the method has nothing to say".
        ids = await _universe(db, {
            "AAA": ["0.0001", "0.0001"], "BBB": ["0.0002", "0.0002"],
        }, audience=uuid4())
        held = frozenset(ids.values())

        decision = await select_carry_basket(
            db.pool,
            entity_ids=list(ids.values()),
            audience_user_id=uuid4(),
            as_of=NOW,
            held=held,
            enter_rank=1,
            exit_rank=2,
        )

        assert decision.abstention == ABSTAIN_NO_COVERAGE
        assert decision.held == held
        assert decision.entered == frozenset()
        assert decision.exited == frozenset()


class TestTheScore:
    async def test_a_name_is_ranked_on_its_mean_rate_not_its_total(self, db):
        """A sum would rank a name up for settling more often rather than for
        paying more -- a venue on a different cadence, or a gap in coverage,
        would decide the basket.

        BUSY prints four small rates totalling more than QUIET's two large ones;
        on the mean QUIET wins, on the sum BUSY would.
        """
        ids = await _universe(db, {
            "AAA": ["0.0001", "0.0001"], "BBB": ["0.0001", "0.0001"],
            "CCC": ["0.0001", "0.0001"], "DDD": ["0.0001", "0.0001"],
            "BUSY": ["0.0003", "0.0003", "0.0003", "0.0003"],
            "QUIET": ["0.0005", "0.0005"],
        })

        decision = await select_carry_basket(
            db.pool,
            entity_ids=list(ids.values()),
            audience_user_id=None,
            as_of=NOW,
            held=(),
            enter_rank=1,
            exit_rank=3,
        )

        assert decision.held == frozenset({ids["QUIET"]})
        assert decision.scores[ids["QUIET"]] > decision.scores[ids["BUSY"]]

    async def test_a_name_with_too_few_settlements_is_not_in_the_universe(self, db):
        # A mean of one observation is the current level, not a trailing
        # average, and the current level is exactly what a carry signal must not
        # chase.
        ids = await _universe(db, {
            "AAA": ["0.0001", "0.0001"], "BBB": ["0.0002", "0.0002"],
            "CCC": ["0.0003", "0.0003"], "DDD": ["0.0004", "0.0004"],
            "EEE": ["0.0005", "0.0005"], "FFF": ["0.0006", "0.0006"],
        })
        thin = await _seed(db, "THIN", ["9.9999"])

        decision = await select_carry_basket(
            db.pool,
            entity_ids=[*ids.values(), thin],
            audience_user_id=None,
            as_of=NOW,
            held=(),
            enter_rank=2,
            exit_rank=4,
        )

        assert thin not in decision.scores
        assert thin not in decision.held


class TestTheDecisionStatesItsTurnover:
    async def test_entered_and_exited_are_what_the_cost_model_prices(self, db):
        """A decision that does not state its own turnover cannot be graded,
        and turnover is the entire difference between the two numbers in
        Finding 9.
        """
        ids = await _universe(db, {
            "AAA": ["0.0001", "0.0001"], "BBB": ["0.0002", "0.0002"],
            "CCC": ["0.0003", "0.0003"], "DDD": ["0.0004", "0.0004"],
            "EEE": ["0.0005", "0.0005"], "FFF": ["0.0006", "0.0006"],
        })
        # Holding the worst name and one that will be retained by hysteresis.
        held = frozenset({ids["AAA"], ids["DDD"]})

        decision = await select_carry_basket(
            db.pool,
            entity_ids=list(ids.values()),
            audience_user_id=None,
            as_of=NOW,
            held=held,
            enter_rank=2,
            exit_rank=4,
        )

        assert decision.abstention is None
        # DDD ranks third: outside the top 2 but inside the exit band, so it
        # keeps its slot. AAA ranks last and leaves. That fills one of the two
        # slots, so only the single best entrant is bought.
        assert decision.held == frozenset({ids["DDD"], ids["FFF"]})
        assert decision.entered == frozenset({ids["FFF"]})
        assert decision.exited == frozenset({ids["AAA"]})
        # The two sets partition the change: nothing is both, and together with
        # the retained names they account for the whole book.
        assert not (decision.entered & decision.exited)
        assert decision.held - decision.entered == held - decision.exited
