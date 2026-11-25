from datetime import date
from types import SimpleNamespace

import pytest

from onepyproject.workcalendar import (MAX_SPAN_DAYS, MAX_TOTAL_DAYS, Period, WorkCalendar, check_no_overlap,
                                       format_period, parse_period, parse_periods, uses_period_syntax,
                                       work_calendar)


@pytest.mark.parametrize("text, expected", [
    ("2026-12-25", Period(date(2026, 12, 25), date(2026, 12, 25))),
    ("  2026-12-25  ", Period(date(2026, 12, 25), date(2026, 12, 25))),
    ("2026-12-25 Christmas Day", Period(date(2026, 12, 25), date(2026, 12, 25), "Christmas Day")),
    ("2026-12-21..2027-01-01", Period(date(2026, 12, 21), date(2027, 1, 1))),
    ("2026-12-21..2027-01-01 Christmas  shutdown ", Period(date(2026, 12, 21), date(2027, 1, 1), "Christmas  shutdown")),
    ("2026-04-25..2026-04-25", Period(date(2026, 4, 25), date(2026, 4, 25))),
    ("2026-06-08 King's Official Birthday", Period(date(2026, 6, 8), date(2026, 6, 8), "King's Official Birthday")),
])
def test_parse_period(text, expected):
    assert parse_period(text) == expected


@pytest.mark.parametrize("text", [
    "", "   ", "not-a-date", "2026-13-01", "2026-02-30", "2026-12-21..", "..2026-12-21", "2026-12-21..2026-12-22..2026-12-23",
    "2026-12-21 .. 2027-01-01 Christmas", "2026-12-21 ..2027-01-01",
])
def test_bad_periods_are_rejected_with_an_example(text):
    with pytest.raises(ValueError, match="date period"):
        parse_period(text)


def test_a_range_must_not_end_before_it_starts():
    with pytest.raises(ValueError, match="ends before it starts"):
        parse_period("2027-01-01..2026-12-21")


def test_one_entry_has_a_size_limit():
    assert parse_period("2026-01-01..2035-12-31").length <= MAX_SPAN_DAYS
    with pytest.raises(ValueError, match="longer than"):
        parse_period("2026-01-01..2040-01-01")
    with pytest.raises(ValueError, match="longer than"):
        parse_period("0001-01-01..9999-12-31")


def test_names_are_limited_and_cannot_hold_separators_or_control_characters():
    with pytest.raises(ValueError, match="name"):
        parse_period("2026-12-25 " + "x" * 121)
    with pytest.raises(ValueError, match="name"):
        parse_period("2026-12-25 Christmas\x00Day")
    with pytest.raises(ValueError, match="date period must be text"):
        parse_period(20261225)


@pytest.mark.parametrize("text", [
    "2026-12-25", "2026-12-25 Christmas Day", "2026-12-21..2027-01-01", "2026-12-21..2027-01-01 Christmas shutdown",
])
def test_format_period_round_trips(text):
    assert format_period(parse_period(text)) == text


def test_format_period_writes_a_canonical_date():
    assert format_period(parse_period("20261225")) == "2026-12-25"
    assert format_period(parse_period("2026-12-25..2026-12-25 Day")) == "2026-12-25 Day"


def test_parse_periods_splits_on_lines_and_commas_and_skips_blanks():
    periods = parse_periods("2026-12-24, 2026-12-25 Christmas Day\n\n2027-01-01..2027-01-02\n")
    assert [format_period(p) for p in periods] == ["2026-12-24", "2026-12-25 Christmas Day", "2027-01-01..2027-01-02"]
    assert parse_periods("") == [] and parse_periods("\n ,\n") == []


def test_parse_periods_names_the_bad_entry():
    with pytest.raises(ValueError, match="not-a-date"):
        parse_periods("2026-12-25\nnot-a-date")


def test_parse_periods_rejects_overlaps_and_duplicates():
    with pytest.raises(ValueError, match="overlaps"):
        parse_periods("2026-12-21..2027-01-01\n2026-12-25")
    with pytest.raises(ValueError, match="overlaps"):
        parse_periods("2026-12-25\n2026-12-25")
    with pytest.raises(ValueError, match="overlaps"):
        parse_periods("2026-12-21..2026-12-25, 2026-12-25..2026-12-28")
    # Adjacent periods do not overlap.
    assert len(parse_periods("2026-12-21..2026-12-24\n2026-12-25..2026-12-28")) == 2
    check_no_overlap([])


def test_calendar_is_monday_to_friday_without_holidays():
    calendar = WorkCalendar()
    assert [calendar.is_workday(date(2026, 11, day)) for day in range(16, 23)] == [True] * 5 + [False] * 2


def test_holidays_and_working_days_change_which_dates_are_worked():
    calendar = WorkCalendar(holidays=frozenset({date(2026, 11, 17)}), workdays=frozenset({date(2026, 11, 21)}))
    assert not calendar.is_workday(date(2026, 11, 17))       # a holiday on a weekday
    assert calendar.is_workday(date(2026, 11, 21))           # a working Saturday
    assert not calendar.is_workday(date(2026, 11, 22))       # the Sunday stays off
    assert calendar.is_workday(date(2026, 11, 18))


def test_a_date_cannot_be_a_holiday_and_a_working_day():
    with pytest.raises(ValueError, match="2026-11-21 cannot be both"):
        WorkCalendar(holidays=frozenset({date(2026, 11, 21)}), workdays=frozenset({date(2026, 11, 21)}))


def test_work_calendar_expands_ranges_from_a_plan():
    plan = SimpleNamespace(holidays=["2026-12-21..2027-01-01 Christmas", "2026-04-03"], working_days=["2026-10-31"])
    calendar = work_calendar(plan)
    assert len(calendar.holidays) == 13 and date(2026, 12, 28) in calendar.holidays
    assert calendar.workdays == {date(2026, 10, 31)}
    assert calendar.is_workday(date(2026, 10, 31)) and not calendar.is_workday(date(2026, 12, 23))


def test_work_calendar_merges_overlaps_so_older_plans_still_load():
    plan = SimpleNamespace(holidays=["2026-12-25", "2026-12-25", "2026-12-24..2026-12-26"], working_days=[])
    assert len(work_calendar(plan).holidays) == 3


def test_work_calendar_without_a_working_days_attribute_reads_as_none():
    assert work_calendar(SimpleNamespace(holidays=["2026-12-25"])).workdays == frozenset()


def test_work_calendar_reports_which_list_is_wrong_and_conflicts():
    with pytest.raises(ValueError, match="Holidays: Invalid date period 'junk'"):
        work_calendar(SimpleNamespace(holidays=["junk"], working_days=[]))
    with pytest.raises(ValueError, match="Working days: Invalid date period"):
        work_calendar(SimpleNamespace(holidays=[], working_days=["junk"]))
    with pytest.raises(ValueError, match="cannot be both"):
        work_calendar(SimpleNamespace(holidays=["2026-10-30..2026-11-02"], working_days=["2026-10-31"]))


def test_work_calendar_caps_the_total_number_of_days():
    entries = [f"{2000 + 10 * i}-01-01..{2000 + 10 * i}-12-31" for i in range(MAX_TOTAL_DAYS // 365 + 2)]
    with pytest.raises(ValueError, match="more than"):
        work_calendar(SimpleNamespace(holidays=entries, working_days=[]))


def test_uses_period_syntax_only_for_entries_older_versions_cannot_read():
    assert not uses_period_syntax([]) and not uses_period_syntax(["2026-12-25", " 2027-01-01 "])
    assert uses_period_syntax(["2026-12-25", "2026-12-26 Boxing Day"])
    assert uses_period_syntax(["2026-12-21..2027-01-01"])
