from datetime import datetime
import json

import pytest

from onepyproject.dependencies import parse_dependency, parse_display_predecessors, parse_predecessors, remap_predecessors, task_numbers
from onepyproject.model import Plan, Task, completion_percentages, indent, load_plan, outdent, save_plan, schedule


def test_display_ids_remap_dependency_text_without_changing_precision():
    tasks = [Task(30, "A"), Task(2, "B"), Task(100, "C")]
    mapping = task_numbers(tasks)
    assert mapping == {30: 1, 2: 2, 100: 3}
    original = [30, "100SS+1.234567890123456789e-3d", " 2FF-50%"]
    assert remap_predecessors(original, mapping) == [1, "3SS+1.234567890123456789e-3d", " 2FF-50%"]
    assert original == [30, "100SS+1.234567890123456789e-3d", " 2FF-50%"]
    assert parse_display_predecessors("1, 3SS+2h", tasks) == [30, "100SS+2h"]
    with pytest.raises(ValueError, match="Task ID 30 does not exist"):
        parse_display_predecessors("30", tasks)


def test_schedule_errors_use_display_task_numbers():
    plan = Plan(tasks=[Task(10, "Valid"), Task(99, "")])
    with pytest.raises(ValueError, match="Task 2 needs a name"):
        schedule(plan)


def test_working_days_holiday_milestone_and_summary():
    plan = Plan(start="2026-11-13", holidays=["2026-11-16"], tasks=[
        Task(1, "Phase"),
        Task(2, "Design", parent=1, duration=2),
        Task(3, "Approval", parent=1, duration=0, predecessors=[2]),
        Task(4, "Build", duration=1, predecessors=[3]),
    ])
    items = schedule(plan)
    assert items[2].start == datetime(2026, 11, 13, 8)
    assert items[2].finish == datetime(2026, 11, 17, 17)
    assert items[3].start == items[3].finish == items[2].finish
    assert items[4].start == datetime(2026, 11, 18, 8)
    assert items[1].summary and items[1].duration == 2


def test_effort_scheduling_and_fixed_duration():
    plan = Plan(start="2026-11-16", tasks=[
        Task(1, "Effort", mode="work", work=80, resource="Team", units=200),
        Task(2, "Duration", duration=5, resource="Team", units=200),
    ])
    items = schedule(plan)
    assert items[1].duration == 5
    assert items[1].work == 80
    assert items[2].duration == 5
    assert items[2].work == 80


def test_rejects_cycles_and_bad_work():
    with pytest.raises(ValueError, match="cycle"):
        schedule(Plan(tasks=[Task(1, predecessors=[2]), Task(2, predecessors=[1])]))
    with pytest.raises(ValueError, match="needs a resource"):
        schedule(Plan(tasks=[Task(1, mode="work")]))


def test_indent_outdent_preserves_preorder():
    plan = Plan(tasks=[Task(1, "Parent"), Task(2, "A"), Task(3, "B"), Task(4, "Next")])
    indent(plan, 2)
    indent(plan, 3)
    assert [t.parent for t in plan.tasks] == [0, 1, 1, 0]
    outdent(plan, 2)
    assert [(t.uid, t.parent) for t in plan.tasks] == [(1, 0), (3, 1), (2, 0), (4, 0)]
    schedule(plan)


def test_native_file_roundtrip(tmp_path):
    p = Plan(title="Plan", start="2026-11-16", holidays=["2026-12-25"], tasks=[Task(4, "Work", percent_complete=42)])
    path = tmp_path / "plan.pxplan"
    save_plan(p, path)
    assert load_plan(path) == p
    assert json.loads(path.read_text())["version"] == 3


def test_old_native_plan_without_estimated_field_still_opens(tmp_path):
    path = tmp_path / "old.pxplan"
    path.write_text(json.dumps({"format": "projectx-plan", "version": 1,
                                "plan": {"title": "Old", "start": "2026-11-16",
                                         "tasks": [{"uid": 1, "name": "Task"}]}}))
    assert not load_plan(path).tasks[0].estimated
    assert not load_plan(path).tasks[0].percent_complete


def test_progress_validation_and_weighted_summary_rollup():
    plan = Plan(start="2026-11-16", tasks=[Task(1, "Top"), Task(2, "Nested", parent=1),
                 Task(3, "Short", parent=2, duration=1, percent_complete=100),
                 Task(4, "Long", parent=2, duration=3, percent_complete=0),
                 Task(5, "Gate", parent=1, duration=0, percent_complete=100)])
    values = completion_percentages(plan)
    assert values[2] == pytest.approx(25)
    assert values[1] == pytest.approx(43.75)
    for invalid in (-0.1, 100.1, float("nan")):
        plan.tasks[1].percent_complete = invalid
        with pytest.raises(ValueError, match="percent complete"):
            schedule(plan)


@pytest.mark.parametrize("value, kind, lag, unit", [
    ("5", "FS", 0, "d"), ("5FS+5d", "FS", 5, "d"),
    ("5SS+3d", "SS", 3, "d"), ("5FF-1d", "FF", -1, "d"),
    ("5SF+2d", "SF", 2, "d"), ("5FS+50%", "FS", 50, "%"),
    ("5FS+2ed", "FS", 2, "ed"), (" 5ss - .5eh ", "SS", -0.5, "eh"),
    ("5SF+5", "SF", 5, "d"), ("5FS-1.5", "FS", -1.5, "d"),
    ("5FS+1emo", "FS", 1, "emo"),
])
def test_parse_dependency(value, kind, lag, unit):
    link = parse_dependency(value)
    assert (link.uid, link.kind, link.lag, link.unit) == (5, kind, lag, unit)


@pytest.mark.parametrize("value", ["5ZZ+1d", "0FS", "5FS+", "5FS+1fortnight", "5,", "5,5SS", "5FS+-2d"])
def test_reject_invalid_dependencies(value):
    with pytest.raises(ValueError):
        parse_predecessors(value)


@pytest.mark.parametrize("expression, start, finish", [
    ("1", datetime(2026, 11, 20, 8), datetime(2026, 11, 20, 17)),
    ("1FS+1d", datetime(2026, 11, 23, 8), datetime(2026, 11, 23, 17)),
    ("1FS-1d", datetime(2026, 11, 19, 8), datetime(2026, 11, 19, 17)),
    ("1SS+1d", datetime(2026, 11, 17, 8), datetime(2026, 11, 17, 17)),
    ("1FF", datetime(2026, 11, 19, 8), datetime(2026, 11, 19, 17)),
    ("1SF+2d", datetime(2026, 11, 17, 8), datetime(2026, 11, 17, 17)),
    ("1FS+50%", datetime(2026, 11, 24, 8), datetime(2026, 11, 24, 17)),
])
def test_dependency_types_and_working_lags(expression, start, finish):
    plan = Plan(start="2026-11-16", tasks=[
        Task(1, "Predecessor", duration=4), Task(2, "Successor", duration=1, predecessors=[expression]),
    ])
    item = schedule(plan)[2]
    assert (item.start, item.finish) == (start, finish)


def test_elapsed_and_working_lag_differ_across_weekend_and_holiday():
    plan = Plan(start="2026-11-13", holidays=["2026-11-16"], tasks=[
        Task(1, "Predecessor"),
        Task(2, "Elapsed", predecessors=["1FS+2ed"]),
        Task(3, "Working", predecessors=["1FS+2d"]),
    ])
    result = schedule(plan)
    assert result[2].start == datetime(2026, 11, 17, 8)
    assert result[3].start == datetime(2026, 11, 19, 8)


def test_lead_elapsed_units_and_percentage_are_calendar_aware():
    plan = Plan(start="2026-11-16", tasks=[
        Task(1, "Predecessor", earliest="2026-11-20"),
        Task(2, "Working lead", predecessors=["1FS-2d"]),
        Task(3, "Elapsed lead", predecessors=["1SS-2ed"]),
        Task(4, "Elapsed hours", predecessors=["1FS+24eh"]),
        Task(5, "Elapsed minutes", predecessors=["1FS+1440em"]),
        Task(6, "Percentage lead", predecessors=["1FS-50%"]),
    ])
    result = schedule(plan)
    assert result[2].start == datetime(2026, 11, 19, 8)
    assert result[3].start == datetime(2026, 11, 18, 8)
    assert result[4].start == result[5].start == datetime(2026, 11, 23, 8)
    assert result[6].start == datetime(2026, 11, 20, 13)


def test_finish_dependency_after_nonworking_time_and_milestone():
    plan = Plan(start="2026-11-13", tasks=[
        Task(1, "Predecessor"),
        Task(2, "Finish after elapsed weekend", duration=1, predecessors=["1FF+1ed"]),
        Task(3, "Milestone", duration=0, predecessors=["1FF+1ed"]),
    ])
    result = schedule(plan)
    assert result[2].finish >= datetime(2026, 11, 14, 17)
    assert result[2].start == datetime(2026, 11, 13, 8, 0, 6)
    assert result[2].finish == datetime(2026, 11, 16, 8, 0, 6)
    assert result[3].start == result[3].finish == datetime(2026, 11, 16, 8)


def test_multiple_links_and_cycle_detection():
    plan = Plan(start="2026-11-16", tasks=[
        Task(1, "A", duration=2), Task(2, "B", duration=3),
        Task(3, "C", predecessors=["1SS+1d", "2FS+1d"]),
    ])
    assert schedule(plan)[3].start == datetime(2026, 11, 20, 8)
    plan.tasks[0].predecessors = ["3SF-1d"]
    with pytest.raises(ValueError, match="cycle"):
        schedule(plan)


def test_dependency_plan_backward_compatibility(tmp_path):
    plan = Plan(tasks=[Task(1, "A"), Task(2, "B", predecessors=["1SS+3h"])])
    path = tmp_path / "schedule.pxplan"
    save_plan(plan, path)
    assert load_plan(path) == plan
    assert parse_predecessors("1, 2SS+3h") == [1, "2SS+3h"]
    assert parse_predecessors("2SF+5, 3FS-2") == ["2SF+5d", "3FS-2d"]


def _summary_plan(*extra):
    # Phase 1 spans Mon 16 Nov 08:00 to Fri 20 Nov 17:00: A takes two days, then B three.
    return Plan(start="2026-11-16", tasks=[
        Task(1, "Phase"), Task(2, "A", parent=1, duration=2), Task(3, "B", parent=1, duration=3, predecessors=[2]),
        *extra])


def test_summary_task_can_be_a_predecessor_using_its_rolled_up_dates():
    items = schedule(_summary_plan(
        Task(10, "After finish", duration=1, predecessors=[1]),
        Task(11, "With start", duration=1, predecessors=["1SS"]),
        Task(12, "With finish", duration=1, predecessors=["1FF"]),
        Task(13, "Lagged", duration=1, predecessors=["1FS+1d"])))
    assert (items[1].start, items[1].finish) == (datetime(2026, 11, 16, 8), datetime(2026, 11, 20, 17))
    assert items[10].start == datetime(2026, 11, 23, 8)
    assert items[11].start == datetime(2026, 11, 16, 8)
    assert items[12].finish == datetime(2026, 11, 20, 17)
    assert items[13].start == datetime(2026, 11, 24, 8)


def test_link_to_a_summary_task_floors_every_subtask():
    plan = Plan(start="2026-11-16", tasks=[
        Task(1, "Lead in", duration=3),
        Task(2, "Phase", predecessors=[1]), Task(3, "A", parent=2, duration=1),
        Task(4, "B", parent=2, duration=2, predecessors=[3]),
        Task(5, "Nested", parent=2), Task(6, "C", parent=5, duration=1),
        Task(7, "Same-phase SS", duration=1, predecessors=[2]),
    ])
    items = schedule(plan)
    assert items[1].finish == datetime(2026, 11, 18, 17)
    # The link floors A and the nested leaf; B follows A as before.
    assert items[3].start == datetime(2026, 11, 19, 8)
    assert items[4].start == datetime(2026, 11, 20, 8)
    assert items[6].start == datetime(2026, 11, 19, 8)
    assert items[2].start == datetime(2026, 11, 19, 8) and items[2].finish == datetime(2026, 11, 23, 17)
    assert items[7].start == datetime(2026, 11, 24, 8)

    plan.tasks[1].predecessors = ["1SS+1d"]
    items = schedule(plan)
    assert items[3].start == items[6].start == datetime(2026, 11, 17, 8)


def test_a_subtask_keeps_a_later_start_from_its_own_predecessor():
    items = schedule(Plan(start="2026-11-16", tasks=[
        Task(1, "Lead in", duration=1), Task(2, "Long", duration=5),
        Task(3, "Phase", predecessors=[1]), Task(4, "A", parent=3, duration=1, predecessors=[2])]))
    assert items[4].start == datetime(2026, 11, 23, 8)


def test_summary_task_rejects_finish_links_unknown_ids_and_cycles():
    with pytest.raises(ValueError, match="Task 2: a summary task supports only FS and SS"):
        schedule(Plan(tasks=[Task(1, "Lead"), Task(2, "Phase", predecessors=["1FF"]), Task(3, "A", parent=2)]))
    with pytest.raises(ValueError, match="Task 2: a summary task supports only FS and SS"):
        schedule(Plan(tasks=[Task(1, "Lead"), Task(2, "Phase", predecessors=["1SF"]), Task(3, "A", parent=2)]))
    with pytest.raises(ValueError, match="Task 1: predecessor internal ID 99 does not exist"):
        schedule(Plan(tasks=[Task(1, "Phase", predecessors=[99]), Task(2, "A", parent=1)]))
    # A subtask cannot depend on the summary that contains it.
    with pytest.raises(ValueError, match="cycle"):
        schedule(Plan(tasks=[Task(1, "Phase"), Task(2, "A", parent=1, predecessors=[1])]))
    # Linking a summary to a task that depends on one of its subtasks is also a cycle.
    with pytest.raises(ValueError, match="cycle"):
        schedule(Plan(tasks=[Task(1, "Phase", predecessors=[3]), Task(2, "A", parent=1),
                             Task(3, "Next", predecessors=[2])]))


def test_summary_links_round_trip_through_a_saved_plan(tmp_path):
    plan = _summary_plan(Task(10, "After", duration=1, predecessors=[1]))
    plan.tasks[0].predecessors = []
    plan.tasks.insert(0, Task(9, "Lead", duration=1))
    plan.tasks[1].predecessors = [9]
    path = tmp_path / "summary.pxplan"
    save_plan(plan, path)
    assert load_plan(path) == plan


def test_fixed_units_schedules_from_the_stored_duration_and_derives_work():
    items = schedule(Plan(start="2026-11-16", tasks=[
        Task(1, "Units", mode="units", duration=5, resource="Team", units=200),
        Task(2, "Units, nobody assigned", mode="units", duration=2)]))
    assert items[1].duration == 5 and items[1].work == 80
    assert items[2].duration == 2 and items[2].work == 0
    with pytest.raises(ValueError, match="invalid scheduling mode"):
        schedule(Plan(tasks=[Task(1, mode="effort")]))


def _lead_in_plan(**follow_on):
    """Lead in runs 16-18 Nov 2026, so a task linked after it is placed on 19 Nov."""
    return Plan(start="2026-11-16", tasks=[
        Task(1, "Lead in", duration=3),
        Task(2, "Follow-on", duration=2, predecessors=[1], **follow_on),
        Task(3, "After", duration=1, predecessors=[2])])


def test_a_task_with_a_floor_is_still_moved_later_by_its_predecessors():
    items = schedule(_lead_in_plan(earliest="2026-11-17"))
    assert items[2].start == datetime(2026, 11, 19, 8)
    assert items[3].start == datetime(2026, 11, 23, 8)


def test_a_fixed_start_is_not_moved_by_predecessors_and_still_drives_successors():
    items = schedule(_lead_in_plan(earliest="2026-11-17", start_mode="fixed"))
    assert items[2].start == datetime(2026, 11, 17, 8) and items[2].finish == datetime(2026, 11, 18, 17)
    # The successor follows the fixed task, not the lead-in.
    assert items[3].start == datetime(2026, 11, 19, 8)
    # A fixed start can also sit later than the links would put it.
    items = schedule(_lead_in_plan(earliest="2026-11-24", start_mode="fixed"))
    assert items[2].start == datetime(2026, 11, 24, 8)


def test_a_fixed_start_ignores_links_inherited_from_its_summary():
    plan = Plan(start="2026-11-16", tasks=[
        Task(1, "Lead in", duration=3),
        Task(2, "Phase", predecessors=[1]),
        Task(3, "Planned", parent=2, duration=1),
        Task(4, "Started", parent=2, duration=2, earliest="2026-11-16", start_mode="fixed")])
    items = schedule(plan)
    assert items[3].start == datetime(2026, 11, 19, 8)       # the summary link floors this one
    assert items[4].start == datetime(2026, 11, 16, 8)       # but not the fixed one
    assert items[2].start == datetime(2026, 11, 16, 8)       # so the summary starts with it


def test_a_fixed_start_before_the_project_start_is_held_at_the_project_start():
    items = schedule(Plan(start="2026-11-16", tasks=[Task(1, "Early", earliest="2026-11-02", start_mode="fixed")]))
    assert items[1].start == datetime(2026, 11, 16, 8)


def test_a_fixed_task_still_reports_unknown_predecessors_and_cycles():
    with pytest.raises(ValueError, match="Task 1: predecessor internal ID 99 does not exist"):
        schedule(Plan(tasks=[Task(1, "A", earliest="2026-11-16", start_mode="fixed", predecessors=[99])]))
    with pytest.raises(ValueError, match="cycle"):
        schedule(Plan(tasks=[Task(1, "A", earliest="2026-11-16", start_mode="fixed", predecessors=[2]),
                             Task(2, "B", predecessors=[1])]))


def test_fixed_start_validation():
    with pytest.raises(ValueError, match="Task 1: a fixed start needs a date"):
        schedule(Plan(tasks=[Task(1, "A", start_mode="fixed")]))
    with pytest.raises(ValueError, match="Task 1: invalid start type"):
        schedule(Plan(tasks=[Task(1, "A", start_mode="soon")]))


def test_start_conflicts_lists_fixed_tasks_that_start_before_their_predecessors_finish():
    from onepyproject.model import start_conflicts
    plan = _lead_in_plan(earliest="2026-11-17", start_mode="fixed")
    assert start_conflicts(plan) == {2: datetime(2026, 11, 19, 8)}
    assert start_conflicts(plan, schedule(plan)) == {2: datetime(2026, 11, 19, 8)}
    plan.tasks[1].earliest = "2026-11-19"                    # on the date the links give: no conflict
    assert start_conflicts(plan) == {}
    plan.tasks[1].earliest = "2026-11-24"                    # later than the links: no conflict
    assert start_conflicts(plan) == {}
    plan.tasks[1].start_mode = "no_earlier"                  # only fixed tasks are reported
    plan.tasks[1].earliest = "2026-11-17"
    assert start_conflicts(plan) == {}


def test_on_leaf_sees_the_link_only_start_and_can_choose_the_hint():
    plan = _lead_in_plan()
    seen = {}

    def choose(task, free):
        seen[task.uid] = free
        if task.uid == 2:
            task.start_mode, task.earliest = "fixed", "2026-11-17"

    items = schedule(plan, on_leaf=choose)
    # Task 3 is placed after task 2 has been fixed, so it sees the fixed dates, not the lead-in's.
    assert seen == {1: datetime(2026, 11, 16, 8), 2: datetime(2026, 11, 19, 8), 3: datetime(2026, 11, 19, 8)}
    assert items[2].start == datetime(2026, 11, 17, 8)
    assert items[3].start == datetime(2026, 11, 19, 8)


def test_start_mode_round_trips_and_older_plans_load_as_no_earlier(tmp_path):
    plan = _lead_in_plan(earliest="2026-11-17", start_mode="fixed")
    path = tmp_path / "fixed.pxplan"
    save_plan(plan, path)
    assert load_plan(path) == plan
    stored = json.loads(path.read_text(encoding="utf-8"))
    for task in stored["plan"]["tasks"]:
        del task["start_mode"]
    path.write_text(json.dumps(stored), encoding="utf-8")
    assert {task.start_mode for task in load_plan(path).tasks} == {"no_earlier"}
