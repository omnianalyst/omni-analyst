"""Schedule recurrence engine, ported from Actual Budget (MIT).

Source: packages/loot-core/src/shared/schedules.ts and the recurrence
config shape in shared/recurrence.ts. Copyright (c) James Long and Actual
Budget contributors, MIT license.

Config shape (JSONB): {"frequency": "daily"|"weekly"|"monthly"|"yearly",
"start": "YYYY-MM-DD", "end"?: date, "interval": n, "skipWeekend": bool,
"weekendSolveMode": "before"|"after"|"nearest", "patterns": [...]}
- daily: no patterns (every `interval` days)
- weekly: dayOfWeek patterns [{type:"dayOfWeek", value:0-6}]
- monthly: [{type:"dayOfMonth", value:1-31} | {type:"dayOfWeek",
  value:0-6, n:1..5|-1}] (-1 = last such weekday), interval in months
- yearly: {"month":1-12, "day":1-31}, interval in years

Dropped from Actual: none of the solver semantics. The rschedule library
underneath Actual is replaced by a direct solver with the same results
for these frequencies; Actual's extra RSchedule options (BYSETPOS chains)
are not expressible in their config UI and are not ported.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


class ScheduleError(ValueError):
    pass


@dataclass
class Recurrence:
    frequency: str
    start: date
    interval: int = 1
    end: date | None = None
    skip_weekend: bool = False
    weekend_solve_mode: str = "after"
    patterns: list[dict] | None = None
    month: int | None = None
    day: int | None = None

    def __post_init__(self):
        if self.frequency not in {"daily", "weekly", "monthly", "yearly"}:
            raise ScheduleError(f"unsupported frequency {self.frequency!r}")
        if self.interval < 1:
            raise ScheduleError("interval must be >= 1")
        if self.weekend_solve_mode not in {"before", "after", "nearest"}:
            raise ScheduleError(f"unsupported weekendSolveMode {self.weekend_solve_mode!r}")

    @classmethod
    def from_config(cls, config: dict) -> Recurrence:
        try:
            return cls(
                frequency=config["frequency"],
                start=date.fromisoformat(str(config.get("start", "2000-01-01"))[:10]),
                interval=int(config.get("interval", 1)),
                end=date.fromisoformat(str(config["end"])[:10]) if config.get("end") else None,
                skip_weekend=bool(config.get("skipWeekend", False)),
                weekend_solve_mode=config.get("weekendSolveMode", "after"),
                patterns=config.get("patterns") or [],
                month=config.get("month"),
                day=config.get("day"),
            )
        except (KeyError, ValueError) as exc:
            raise ScheduleError(f"invalid recurrence config: {exc}") from exc


def _solve_weekend(day: date, mode: str) -> date:
    if day.weekday() < 5:
        return day
    if mode == "before":
        while day.weekday() >= 5:
            day -= timedelta(days=1)
    elif mode == "after":
        while day.weekday() >= 5:
            day += timedelta(days=1)
    else:
        back = day
        while back.weekday() >= 5:
            back -= timedelta(days=1)
        forward = day
        while forward.weekday() >= 5:
            forward += timedelta(days=1)
        back_delta = (day - back).days
        forward_delta = (forward - day).days
        return back if back_delta <= forward_delta else forward
    return day


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date | None:
    if n == -1:
        day = date(year, month, _days_in_month(year, month))
        while day.weekday() != weekday:
            day -= timedelta(days=1)
        return day
    day = date(year, month, 1)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    day += timedelta(days=7 * (n - 1))
    if day.month != month:
        return None
    return day


def _month_occurrences(rec: Recurrence, year: int, month: int) -> list[date]:
    out: list[date] = []
    for pattern in rec.patterns or []:
        ptype = pattern.get("type")
        if ptype == "dayOfMonth":
            value = int(pattern.get("value", 0))
            if 1 <= value <= _days_in_month(year, month):
                out.append(date(year, month, value))
        elif ptype == "dayOfWeek":
            weekday = int(pattern.get("value", 0)) % 7
            n = int(pattern.get("n", 1))
            found = _nth_weekday_of_month(year, month, weekday, n)
            if found is not None:
                out.append(found)
    out.sort()
    return out


def occurrences_between(rec: Recurrence, lo: date, hi: date) -> list[date]:
    """Every solved occurrence date in [lo, hi], weekend-solved, deduped."""
    if hi < lo or hi < rec.start:
        return []
    out: set[date] = set()
    if rec.frequency == "daily":
        day = rec.start
        while day <= hi:
            if day >= lo:
                out.add(day)
            day += timedelta(days=rec.interval)
    elif rec.frequency == "weekly":
        weekdays = {int(p["value"]) % 7 for p in (rec.patterns or []) if p.get("type") == "dayOfWeek"}
        if not weekdays:
            weekdays = {rec.start.weekday()}
        day = rec.start
        week_anchor = rec.start - timedelta(days=rec.start.toordinal() % 7)
        while day <= hi:
            if day >= lo and day.weekday() in weekdays:
                weeks = ((day - week_anchor).days // 7) % rec.interval
                if weeks == 0:
                    out.add(day)
            day += timedelta(days=1)
    elif rec.frequency == "monthly":
        month = date(rec.start.year, rec.start.month, 1)
        while month <= hi:
            index = (month.year - rec.start.year) * 12 + (month.month - rec.start.month)
            if index % rec.interval == 0 and month >= date(lo.year, lo.month, 1):
                for occurrence in _month_occurrences(rec, month.year, month.month):
                    if lo <= occurrence <= hi:
                        out.add(occurrence)
            if month.month == 12:
                month = month.replace(year=month.year + 1, month=1)
            else:
                month = month.replace(month=month.month + 1)
    else:
        year = rec.start.year
        while date(year, 1, 1) <= hi:
            month = rec.month or rec.start.month
            day_value = rec.day or rec.start.day
            if 1 <= month <= 12 and 1 <= day_value <= _days_in_month(year, month):
                occurrence = date(year, month, day_value)
                if lo <= occurrence <= hi and occurrence >= rec.start:
                    out.add(occurrence)
            year += rec.interval
    solved = {_solve_weekend(d, rec.weekend_solve_mode) if rec.skip_weekend else d for d in out}
    return sorted(
        d
        for d in solved
        if lo <= d <= hi and (rec.end is None or d <= rec.end) and d >= rec.start
    )


def next_occurrence(rec: Recurrence, after: date) -> date | None:
    window = occurrences_between(rec, after + timedelta(days=1), after + timedelta(days=370))
    return window[0] if window else None


POSTED_WINDOW_BEFORE = 4
POSTED_WINDOW_AFTER = 7


def occurrence_is_posted(occurrence: date, tx_dates: list[date]) -> bool:
    """Actual's matching: a posted transaction near the occurrence marks
    it paid. Window: 4 days before to 7 days after."""
    lo = occurrence - timedelta(days=POSTED_WINDOW_BEFORE)
    hi = occurrence + timedelta(days=POSTED_WINDOW_AFTER)
    return any(lo <= d <= hi for d in tx_dates)


def schedule_status(rec: Recurrence, today: date, tx_dates: list[date]) -> dict:
    """Actual's getStatus semantics: upcoming / due / paid / missed /
    completed, for the next occurrence on or after today's month view."""
    window = occurrences_between(rec, today - timedelta(days=60), today + timedelta(days=370))
    if not window:
        return {"status": "completed", "next": None}
    upcoming = [d for d in window if d >= today]
    recent = [d for d in window if d < today]
    if recent:
        last = recent[-1]
        if not occurrence_is_posted(last, tx_dates):
            return {"status": "missed", "next": last.isoformat()}
    if upcoming:
        next_date = upcoming[0]
        if occurrence_is_posted(next_date, tx_dates):
            following = next_occurrence(rec, next_date)
            return {
                "status": "paid",
                "next": following.isoformat() if following else None,
            }
        if next_date == today:
            return {"status": "due", "next": next_date.isoformat()}
        return {"status": "upcoming", "next": next_date.isoformat()}
    return {"status": "completed", "next": None}
