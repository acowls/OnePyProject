import copy
import json

import pytest

from onepyproject.ai.diff import compare_plans
from onepyproject.ai.proposals import FIELDS, ProposalError, apply_operations, make_snapshot, parse_proposal, plan_revision
from onepyproject.model import Plan, Resource, ResourceAssignment, Task, schedule


def operation(op_name, **values):
    result = {field: None for field in FIELDS}
    result.update(op=op_name, **values)
    return result


def proposal(snapshot, *operations, message="Proposed changes", clarification=False):
    return {"schema_version": "1", "snapshot_revision": snapshot.revision, "message": message,
            "needs_clarification": clarification, "operations": list(operations)}


def sample_plan():
    return Plan(title="Sample", start="2026-11-16", resources=[Resource(3, "Ada", 100)], tasks=[
        Task(10, "Design", duration=2, assignments=[ResourceAssignment(3, 100)]),
        Task(40, "Test", duration=1, predecessors=[10]),
    ])


def test_snapshot_is_detached_and_uses_stable_refs_with_display_numbers():
    plan = sample_plan()
    before = copy.deepcopy(plan)
    snapshot = make_snapshot(plan)
    assert plan == before and snapshot.plan is not plan
    assert snapshot.payload["tasks"][1]["ref"] == "task:40"
    assert snapshot.payload["tasks"][1]["display_id"] == 2
    assert snapshot.payload["tasks"][1]["predecessors"][0]["task"] == "task:10"
    assert snapshot.payload["tasks"][0]["assignments"][0]["resource"] == "resource:3"
    assert "path" not in json.dumps(snapshot.payload).lower()


def test_snapshot_includes_progress_as_readonly_context():
    snapshot = make_snapshot(Plan(start="2026-11-16", tasks=[Task(1, "Phase"),
        Task(2, "Child", parent=1, duration=2, percent_complete=50)]))
    assert snapshot.payload["tasks"][1]["percent_complete"] == 50
    assert snapshot.payload["tasks"][0]["calculated"]["percent_complete"] == 50


def test_create_tasks_resources_and_dependencies_on_copy():
    plan = sample_plan()
    baseline = copy.deepcopy(plan)
    snapshot = make_snapshot(plan)
    result = apply_operations(snapshot, proposal(snapshot,
        operation("create_resource", temp_id="new-resource:ben", name="Ben", capacity=75),
        operation("create_task", temp_id="new-task:review", name="Review", before="task:40", duration_days=2),
        operation("assign_resource", target="new-task:review", resource="new-resource:ben", units=50),
        operation("add_dependency", target="new-task:review", predecessor="task:10", kind="SS", lag=2, unit="h"),
        operation("add_dependency", target="task:40", predecessor="new-task:review", kind="FS"),
    ))
    assert plan == baseline and snapshot.plan == baseline
    assert [(task.uid, task.name) for task in result.tasks] == [(10, "Design"), (41, "Review"), (40, "Test")]
    assert result.tasks[1].assignments == [ResourceAssignment(4, 50)]
    assert result.tasks[1].predecessors == ["10SS+2h"]
    assert result.tasks[2].predecessors == [10, 41]
    assert schedule(result)[40].start >= schedule(result)[41].finish
    diff = compare_plans(plan, result)
    assert any(change.field == "Added" and "Review" in change.item for change in diff.changes)
    assert any(change.category == "Calculated" and "Test" in change.item for change in diff.changes)
    assert any(change.field == "Displayed ID" and "Test" in change.item and change.before == "2" and change.after == "3"
               for change in diff.changes)


def test_snapshot_revision_changes_with_any_unsaved_edit():
    plan = sample_plan()
    original = plan_revision(plan)
    plan.tasks[0].duration = 3
    assert plan_revision(plan) != original


def test_ai_generated_durations_do_not_gain_estimate_marks():
    plan = Plan(start="2026-11-16", tasks=[Task(1, "Existing", duration=5, estimated=True),
                                            Task(2, "Unchanged estimate", estimated=True)])
    snapshot = make_snapshot(plan)
    candidate = apply_operations(snapshot, proposal(snapshot,
        operation("update_task", target="task:1", duration_days=3, estimated=True),
        operation("update_task", target="task:1", estimated=True),
        operation("create_task", temp_id="new-task:a", name="New", duration_days=2, estimated=True)))
    assert not candidate.tasks[0].estimated and not candidate.tasks[-1].estimated
    assert candidate.tasks[1].estimated and plan.tasks[0].estimated


def test_ai_can_change_an_existing_estimate_without_changing_duration():
    snapshot = make_snapshot(Plan(start="2026-11-16", tasks=[Task(1, "Task")]))
    candidate = apply_operations(snapshot, proposal(snapshot, operation("update_task", target="task:1", estimated=True)))
    assert candidate.tasks[0].estimated and not snapshot.plan.tasks[0].estimated


def test_ai_echoing_unchanged_duration_preserves_existing_estimate():
    snapshot = make_snapshot(Plan(start="2026-11-16", tasks=[Task(1, "Task", duration=3, estimated=True)]))
    candidate = apply_operations(snapshot, proposal(snapshot,
        operation("update_task", target="task:1", name="Renamed", duration_days=3)))
    assert candidate.tasks[0].name == "Renamed" and candidate.tasks[0].estimated


@pytest.mark.parametrize("operation_list", [
    [operation("update_task", target="task:10", duration_days=-1)],
    [operation("update_task", target="task:999", duration_days=2)],
    [operation("add_dependency", target="task:10", predecessor="task:40")],
    [operation("assign_resource", target="task:40", resource="resource:3", units=-5)],
    [operation("update_task", target="task:10", earliest="2026-11-16 09:00")],
    [operation("update_task", target="task:10", work_hours=8)],
    [operation("update_task", target="task:10", name="Invisible\x00text")],
    [operation("delete_resource", target="resource:3")],
])
def test_invalid_proposals_are_rejected_without_mutation(operation_list):
    plan = sample_plan()
    old = copy.deepcopy(plan)
    snapshot = make_snapshot(plan)
    with pytest.raises(ProposalError):
        apply_operations(snapshot, proposal(snapshot, *operation_list))
    assert plan == old and snapshot.plan == old


def test_deletion_does_not_reuse_internal_ids_or_allow_guessed_new_ids():
    snapshot = make_snapshot(sample_plan())
    with pytest.raises(ProposalError, match="not present"):
        apply_operations(snapshot, proposal(snapshot,
            operation("delete_task", target="task:40"),
            operation("create_task", temp_id="new-task:x", name="Replacement"),
            operation("update_task", target="task:41", name="Guessed")))
    result = apply_operations(snapshot, proposal(snapshot,
        operation("delete_task", target="task:40"),
        operation("create_task", temp_id="new-task:x", name="Replacement")))
    assert [task.uid for task in result.tasks] == [10, 41]


def test_move_preserves_subtree_and_delete_removes_dependent_links():
    plan = Plan(start="2026-11-16", tasks=[Task(1, "Phase"), Task(2, "Child", parent=1),
                 Task(3, "Other", predecessors=[2])])
    snapshot = make_snapshot(plan)
    moved = apply_operations(snapshot, proposal(snapshot, operation("move_task", target="task:2")))
    assert [(task.uid, task.parent) for task in moved.tasks] == [(1, 0), (3, 0), (2, 0)]
    assert moved.tasks[1].predecessors == [2]
    removed = apply_operations(snapshot, proposal(snapshot, operation("delete_task", target="task:1")))
    assert [task.uid for task in removed.tasks] == [3] and removed.tasks[0].predecessors == []
    diff = compare_plans(plan, removed)
    assert any(c.field == "Removed" and "Child" in c.item for c in diff.changes)
    assert any(c.field == "Predecessors" and "Other" in c.item for c in diff.changes)


def test_cannot_set_calculated_fields_on_new_summary_task():
    snapshot = make_snapshot(Plan(start="2026-11-16"))
    with pytest.raises(ProposalError, match="Summary task"):
        apply_operations(snapshot, proposal(snapshot,
            operation("create_task", temp_id="new-task:parent", name="Phase", duration_days=5),
            operation("create_task", temp_id="new-task:child", name="Child", parent="new-task:parent")))


def test_diff_includes_indirect_schedule_and_resource_warning_changes():
    plan = sample_plan()
    snapshot = make_snapshot(plan)
    after = apply_operations(snapshot, proposal(snapshot,
        operation("update_task", target="task:10", duration_days=3),
        operation("assign_resource", target="task:40", resource="resource:3", units=150)))
    diff = compare_plans(plan, after)
    assert any(c.category == "Direct" and c.field == "Duration days" for c in diff.changes)
    assert any(c.category == "Calculated" and c.field == "Scheduled start" and "Test" in c.item for c in diff.changes)
    assert any(c.field == "Overallocation" and "Ada" in c.item for c in diff.changes)


def test_diff_does_not_round_distinct_values():
    plan = sample_plan()
    after = copy.deepcopy(plan)
    plan.tasks[0].duration = 1.1234561
    after.tasks[0].duration = 1.1234562
    diff = compare_plans(plan, after)
    change = next(c for c in diff.changes if c.field == "Duration days")
    assert (change.before, change.after) == ("1.1234561", "1.1234562")


def test_legacy_assignments_are_not_silently_migrated():
    plan = Plan(start="2026-11-16", tasks=[Task(1, "A", resource="Ada"), Task(2, "B", resource="Ada")])
    snapshot = make_snapshot(plan)
    result = apply_operations(snapshot, proposal(snapshot,
        operation("create_resource", temp_id="new-resource:ben", name="Ben")))
    assert [task.resource for task in result.tasks] == ["Ada", "Ada"]
    assert snapshot.payload["overallocation_warnings"][0]["resource"] is None
    diff = compare_plans(plan, result)
    assert not any(change.field == "Overallocation" for change in diff.changes)


def test_clarification_proposal_does_not_change_the_project():
    snapshot = make_snapshot(sample_plan())
    result = parse_proposal(json.dumps(proposal(snapshot, message="Which task?", clarification=True)), snapshot.revision)
    assert result["needs_clarification"] and not result["operations"]
    assert apply_operations(snapshot, result) == snapshot.plan


def test_proposal_parser_fails_closed_for_invalid_content():
    snapshot = make_snapshot(sample_plan())
    good = proposal(snapshot, operation("update_task", target="task:10", duration_days=3))
    assert parse_proposal(json.dumps(good), snapshot.revision) == good
    for raw in ("not JSON", '{"x":1,"x":2}', json.dumps({**good, "extra": "unexpected"}),
                json.dumps({**good, "snapshot_revision": "wrong"}),
                json.dumps({**good, "message": "\ud800"}),
                json.dumps({**good, "needs_clarification": True}),
                json.dumps(proposal(snapshot, operation("update_task", target="task:10", duration_days=True)))):
        with pytest.raises(ProposalError):
            parse_proposal(raw, snapshot.revision)


def test_clearing_the_earliest_date_of_a_fixed_task_releases_the_fixed_start():
    plan = sample_plan()
    plan.tasks[1].earliest, plan.tasks[1].start_mode = "2026-11-17", "fixed"
    snapshot = make_snapshot(plan)
    candidate = apply_operations(snapshot, proposal(snapshot, operation("update_task", target="task:40", earliest="")))
    assert (candidate.tasks[1].earliest, candidate.tasks[1].start_mode) == ("", "no_earlier")
    moved = apply_operations(snapshot, proposal(snapshot, operation("update_task", target="task:40",
                                                                    earliest="2026-11-30")))
    assert (moved.tasks[1].earliest, moved.tasks[1].start_mode) == ("2026-11-30", "fixed")
    assert schedule(moved)[40].start.date().isoformat() == "2026-11-30"


def test_snapshot_and_diff_carry_holiday_periods_and_read_only_working_days():
    plan = sample_plan()
    plan.holidays = ["2026-11-17 Day off"]
    plan.working_days = ["2026-11-21 Catch-up"]
    snapshot = make_snapshot(plan)
    assert snapshot.payload["project"]["holidays"] == ["2026-11-17 Day off"]
    assert snapshot.payload["project"]["working_days"] == ["2026-11-21 Catch-up"]
    assert "working_days" in snapshot.payload["calendar"]
    result = apply_operations(snapshot, proposal(snapshot, operation(
        "update_project", holidays=["2026-12-21..2027-01-01 Christmas", "2026-11-17 Day off"])))
    assert result.holidays == ["2026-12-21..2027-01-01 Christmas", "2026-11-17 Day off"]
    assert result.working_days == ["2026-11-21 Catch-up"]               # the AI cannot change working days
    changes = compare_plans(plan, result).changes
    assert [(c.item, c.field) for c in changes if c.category == "Direct"] == [("Project", "Holidays")]
    result.working_days = []
    assert [c.field for c in compare_plans(plan, result).changes if c.item == "Project"] == ["Holidays", "Working days"]


@pytest.mark.parametrize("holidays, message", [
    (["2026-12-25", "2026-12-25"], "overlaps"),
    (["2026-12-21..2027-01-01", "2026-12-25 Christmas Day"], "overlaps"),
    (["not-a-date"], "Invalid holiday list"),
    (["2027-01-01..2026-12-21"], "ends before it starts"),
    (["2026-12-25 " + "x" * 130], "Invalid holiday list"),
])
def test_ai_holiday_edits_are_validated_like_the_settings_dialog(holidays, message):
    snapshot = make_snapshot(sample_plan())
    with pytest.raises(ProposalError, match=message):
        apply_operations(snapshot, proposal(snapshot, operation("update_project", holidays=holidays)))


def test_an_ai_holiday_that_covers_a_working_day_is_rejected_as_an_invalid_project():
    plan = sample_plan()
    plan.working_days = ["2026-11-21"]
    snapshot = make_snapshot(plan)
    with pytest.raises(ProposalError, match="2026-11-21 cannot be both a holiday and a working day"):
        apply_operations(snapshot, proposal(snapshot, operation("update_project", holidays=["2026-11-20..2026-11-22"])))
