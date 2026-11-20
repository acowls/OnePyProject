import copy

from onepyproject.ai.preview import build_preview
from onepyproject.ai.proposals import FIELDS, apply_operations, make_snapshot
from onepyproject.app import display_time
from onepyproject.model import Plan, Resource, Task


def test_preview_preserves_deleted_hierarchy_and_merged_row_order():
    before = Plan(start="2026-11-16", tasks=[Task(1, "Phase"), Task(2, "Removed", parent=1),
                  Task(3, "Removed child", parent=2), Task(4, "Kept", parent=1), Task(5, "Last")])
    after = copy.deepcopy(before)
    after.tasks = [after.tasks[0], after.tasks[3], Task(6, "New", parent=1), after.tasks[4]]
    preview = build_preview(before, after, display_time)
    assert [(r.uid, r.parent, r.status) for r in preview.rows] == [
        (1, 0, "unchanged"), (2, 1, "removed"), (3, 2, "removed"),
        (4, 1, "changed"), (6, 1, "added"), (5, 0, "changed")]
    assert [t.uid for t in before.tasks] == [1, 2, 3, 4, 5]


def test_preview_marks_direct_and_indirect_date_changes():
    before = Plan(start="2026-11-16", tasks=[Task(1, "First", duration=4), Task(2, "Second", predecessors=[1])])
    after = copy.deepcopy(before)
    after.tasks[0].duration = 2
    rows = build_preview(before, after, display_time).rows
    assert rows[0].status == "changed" and 2 in rows[0].changed_columns and 4 in rows[0].changed_columns
    assert rows[1].status == "changed" and {3, 4} <= rows[1].changed_columns
    assert rows[0].before.finish != rows[0].after.finish


def test_preview_marks_leaf_and_rolled_up_progress_changes():
    before = Plan(start="2026-11-16", tasks=[Task(1, "Phase"), Task(2, "Child", parent=1, duration=2)])
    after = copy.deepcopy(before)
    after.tasks[1].percent_complete = 50
    rows = build_preview(before, after, display_time).rows
    assert rows[0].status == rows[1].status == "changed"
    assert 9 in rows[0].changed_columns and 9 in rows[1].changed_columns
    assert rows[0].values[9] == rows[1].values[9] == "50.0%"


def test_refinement_never_reuses_deleted_baseline_ids():
    before = Plan(start="2026-11-16", tasks=[Task(1, "Old")], resources=[Resource(1, "Old resource")])
    staged = Plan(start=before.start)
    snapshot = make_snapshot(staged)
    def operation(op_name, **kwargs):
        item = {key: None for key in FIELDS}
        item.update(op=op_name, **kwargs)
        return item
    proposal = {"schema_version": "1", "snapshot_revision": snapshot.revision, "message": "Add replacements",
                "needs_clarification": False,
                "operations": [operation("create_task", temp_id="new-task:a", name="New"),
                               operation("create_resource", temp_id="new-resource:a", name="New resource")]}
    candidate = apply_operations(snapshot, proposal, reserved_task_ids=[1], reserved_resource_ids=[1])
    assert candidate.tasks[0].uid == 2 and candidate.resources[0].uid == 2
    assert {row.uid: row.status for row in build_preview(before, candidate, display_time).rows} == {1: "removed", 2: "added"}


def test_snapshot_shows_the_start_rule_and_preview_flags_a_changed_rule():
    before = Plan(start="2026-11-16", tasks=[Task(1, "Lead in", duration=3),
                                             Task(2, "Follow-on", duration=2, predecessors=[1],
                                                  earliest="2026-11-17", start_mode="fixed")])
    snapshot = make_snapshot(before)
    assert [row["start_rule"] for row in snapshot.payload["tasks"]] == ["no_earlier", "fixed"]
    assert snapshot.payload["tasks"][1]["earliest_date"] == "2026-11-17"

    after = copy.deepcopy(before)
    after.tasks[1].start_mode = "no_earlier"
    row = build_preview(before, after, display_time).rows[1]
    assert row.status == "changed" and 3 in row.changed_columns
    assert "Start rule: Fixed → No earlier than" in row.details
