"""Working-time exceptions to the standard week: holiday periods and extra working days.

The week itself is fixed (Monday-Friday, 08:00-12:00 and 13:00-17:00). A plan lists holiday
periods, which are not worked, and working days, which are worked with the standard hours even
on a weekend. Both are stored as text entries like predecessors are: ``2026-12-25``,
``2026-12-21..2027-01-01`` or either followed by a name, ``2026-12-21..2027-01-01 Christmas``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import re
from typing import Iterator

MAX_SPAN_DAYS = 3660          # one entry may cover about ten years
MAX_TOTAL_DAYS = 20000        # all entries of one kind together
MAX_NAME_LENGTH = 120
_FORBIDDEN_NAME = re.compile(r"[,\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Period:
    start: date
    finish: date
    name: str = ""

    def days(self) -> Iterator[date]:
        day = self.start
        while day <= self.finish:
            yield day
            day += timedelta(days=1)

    @property
    def length(self) -> int:
        return (self.finish - self.start).days + 1


def parse_period(value: str) -> Period:
    """Read one entry, such as ``2026-12-25`` or ``2026-12-21..2027-01-01 Christmas shutdown``."""
    if not isinstance(value, str):
        raise ValueError("A date period must be text such as 2026-12-25 or 2026-12-21..2027-01-01")
    text = value.strip()
    example = "use e.g. 2026-12-25 or 2026-12-21..2027-01-01 Christmas"
    if not text:
        raise ValueError(f"Empty date period; {example}")
    parts = text.split(None, 1)
    span, name = parts[0], (parts[1].strip() if len(parts) > 1 else "")
    if name.startswith(".."):
        raise ValueError(f"Invalid date period {text!r}; write a range without spaces around '..'")
    ends = span.split("..")
    try:
        if len(ends) > 2:
            raise ValueError
        start = date.fromisoformat(ends[0])
        finish = date.fromisoformat(ends[1]) if len(ends) == 2 else start
    except ValueError:
        raise ValueError(f"Invalid date period {text!r}; {example}") from None
    if finish < start:
        raise ValueError(f"Date period {text!r} ends before it starts")
    if (finish - start).days + 1 > MAX_SPAN_DAYS:
        raise ValueError(f"Date period {text!r} is longer than {MAX_SPAN_DAYS} days")
    if len(name) > MAX_NAME_LENGTH or _FORBIDDEN_NAME.search(name):
        raise ValueError(f"Date period {text!r}: a name may not contain commas or control characters "
                         f"and may be at most {MAX_NAME_LENGTH} characters")
    return Period(start, finish, name)


def format_period(period: Period) -> str:
    span = period.start.isoformat()
    if period.finish != period.start:
        span += f"..{period.finish.isoformat()}"
    return f"{span} {period.name}" if period.name else span


def parse_periods(text: str) -> list[Period]:
    """Read entries one per line or separated by commas, checking that none overlap."""
    periods = [parse_period(part) for line in text.splitlines() for part in line.split(",") if part.strip()]
    check_no_overlap(periods)
    return periods


def check_no_overlap(periods: list[Period], label: str = "Date period") -> None:
    previous = None
    for period in sorted(periods, key=lambda item: (item.start, item.finish)):
        if previous is not None and period.start <= previous.finish:
            raise ValueError(f"{label} {format_period(period)!r} overlaps {format_period(previous)!r}")
        previous = period


@dataclass(frozen=True)
class WorkCalendar:
    """Which dates are worked: Monday-Friday except holidays, plus any extra working days."""
    holidays: frozenset[date] = frozenset()
    workdays: frozenset[date] = frozenset()

    def __post_init__(self):
        both = self.holidays & self.workdays
        if both:
            raise ValueError(f"{min(both).isoformat()} cannot be both a holiday and a working day")

    def is_workday(self, day: date) -> bool:
        if day in self.workdays:
            return True
        return day.weekday() < 5 and day not in self.holidays


def _days(entries, label: str) -> set[date]:
    days: set[date] = set()
    for entry in entries:
        try:
            period = parse_period(entry)
        except ValueError as exc:
            raise ValueError(f"{label}: {exc}") from None
        days.update(period.days())
        if len(days) > MAX_TOTAL_DAYS:
            raise ValueError(f"{label}: more than {MAX_TOTAL_DAYS} days are listed")
    return days


def work_calendar(plan) -> WorkCalendar:
    """The calendar a plan's holiday and working-day entries describe.

    Entries that overlap are merged. Use ``parse_periods`` to reject overlaps when a person
    types them in.
    """
    return WorkCalendar(frozenset(_days(plan.holidays, "Holidays")),
                        frozenset(_days(getattr(plan, "working_days", ()), "Working days")))


def uses_period_syntax(entries) -> bool:
    """True when an entry needs more than a single ISO date, so older versions cannot read it."""
    return any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry.strip()) for entry in entries)
