from datetime import datetime
import json

import pytest

from onepyproject.model import Plan, Resource, ResourceAssignment, Task, indent, load_plan, save_plan, schedule
from onepyproject.resources import format_task_resources, migrate_legacy_resources, overallocation_warnings, set_task_resources


def test_multiple_resource_effort_and_duration():
    plan = Plan(start="2026-11-16", resources=[Resource(1, "Ada"), Resource(2, "Ben")], tasks=[
        Task(1, "Fixed work", mode="work", work=80,
             assignments=[ResourceAssignment(1, 100), ResourceAssignment(2, 100)]),
        Task(2, "Fixed duration", duration=5,
             assignments=[ResourceAssignment(1, 50), ResourceAssignment(2, 100)]),
    ])
    calculated = schedule(plan)
    assert calculated[1].duration == 5 and calculated[1].work == 80
    assert calculated[2].duration == 5 and calculated[2].work == 60


def test_old_file_migrates_resources_without_losing_allocation(tmp_path):
    path = tmp_path / "old.pxplan"
    path.write_text(json.dumps({"format": "projectx-plan", "version": 1, "plan": {
        "title": "Legacy", "start": "2026-11-16", "tasks": [
            {"uid": 1, "name": "A", "resource": "Ada", "units": 50},
            {"uid": 2, "name": "B", "resource": "ada", "units": 100},
        ]}}))
    plan = load_plan(path)
    assert [(r.uid, r.name) for r in plan.resources] == [(1, "Ada"), (2, "ada")]
    assert [task.assignments for task in plan.tasks] == [[ResourceAssignment(1, 50)], [ResourceAssignment(2, 100)]]
    assert all(not task.resource for task in plan.tasks)
    assert schedule(plan)[1].work == 4
    save_plan(plan, path)
    assert json.loads(path.read_text())["version"] == 3
    assert load_plan(path) == plan


def test_assignment_text_and_renamed_resource():
    plan = Plan(tasks=[Task(1, "Task")])
    set_task_resources(plan, plan.tasks[0], '"Ada, Senior[50%]", Ben')
    assert [r.name for r in plan.resources] == ["Ada, Senior", "Ben"]
    assert [a.units for a in plan.tasks[0].assignments] == [50, 100]
    assert format_task_resources(plan, plan.tasks[0]) == '"Ada, Senior[50%]", Ben'
    plan.resources[0].name = "Ada"
    assert format_task_resources(plan, plan.tasks[0]) == "Ada[50%], Ben"
    set_task_resources(plan, plan.tasks[0], "Ben,Ada[75%]")
    assert format_task_resources(plan, plan.tasks[0]) == "Ben, Ada[75%]"
    with pytest.raises(ValueError, match="more than once"):
        set_task_resources(plan, plan.tasks[0], "Ben,ben")


def test_overallocation_does_not_move_tasks_and_honours_boundaries():
    plan = Plan(start="2026-11-16", resources=[Resource(1, "Ada", max_units=100)], tasks=[
        Task(1, "First", assignments=[ResourceAssignment(1, 75)]),
        Task(2, "Overlap", assignments=[ResourceAssignment(1, 50)]),
        Task(3, "After", predecessors=[1], assignments=[ResourceAssignment(1, 75)]),
        Task(4, "Gate", duration=0, assignments=[ResourceAssignment(1, 100)]),
    ])
    before = schedule(plan)
    warnings = overallocation_warnings(plan)
    assert warnings[1][0].units == 125 and warnings[1][0].capacity == 100
    assert warnings[1][0].start == datetime(2026, 11, 16, 8)
    assert warnings[1][0].finish == datetime(2026, 11, 16, 17)
    assert schedule(plan) == before
    assert len(warnings[1]) == 1  # The next-day task and zero-duration milestone don't overlap.


def test_resource_validation_and_legacy_migration():
    plan = Plan(tasks=[Task(1, "Legacy", resource="Ada", units=25)])
    assert schedule(plan)[1].work == 2
    migrate_legacy_resources(plan)
    assert plan.tasks[0].assignments == [ResourceAssignment(1, 25)]
    assert schedule(plan)[1].work == 2
    plan.tasks[0].assignments.append(ResourceAssignment(1, 25))
    with pytest.raises(ValueError, match="unique"):
        schedule(plan)
    plan.tasks[0].assignments = [ResourceAssignment(99, 25)]
    with pytest.raises(ValueError, match="does not exist"):
        schedule(plan)
    plan.tasks[0].assignments = []
    plan.resources.append(Resource(2, "Ada"))
    with pytest.raises(ValueError, match="Duplicate resource"):
        schedule(plan)


def test_precise_percentages_and_tiny_fixed_work_allocation():
    original = 37.12345678912345
    plan = Plan(resources=[Resource(1, "Ada", 100.12345678912345)],
                tasks=[Task(1, "Task", assignments=[ResourceAssignment(1, original)])])
    text = format_task_resources(plan, plan.tasks[0])
    set_task_resources(plan, plan.tasks[0], text)
    assert plan.tasks[0].assignments[0].units == original
    plan.tasks[0].mode = "work"
    plan.tasks[0].assignments[0].units = 5e-324
    with pytest.raises(ValueError, match="outside the supported range"):
        schedule(plan)


def test_resource_names_differing_only_by_case_remain_distinct():
    plan = Plan(resources=[Resource(1, "Ada"), Resource(2, "ada")], tasks=[Task(1, "Task")])
    set_task_resources(plan, plan.tasks[0], "Ada,ada")
    assert plan.tasks[0].assignments == [ResourceAssignment(1), ResourceAssignment(2)]
    with pytest.raises(ValueError, match="Ambiguous resource"):
        set_task_resources(plan, plan.tasks[0], "ADA")


def test_indent_does_not_silently_turn_assigned_task_into_summary():
    plan = Plan(resources=[Resource(1, "Ada")], tasks=[
        Task(1, "Assigned", assignments=[ResourceAssignment(1)]), Task(2, "Child"),
    ])
    with pytest.raises(ValueError, match="before making it a summary"):
        indent(plan, 2)
    assert plan.tasks[1].parent == 0
