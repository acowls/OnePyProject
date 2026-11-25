from pathlib import Path
from datetime import datetime

import pytest

from onepyproject.dependencies import parse_dependency
from onepyproject.model import Plan, Resource, ResourceAssignment, Task, schedule
from onepyproject.mpp import export_mpp, import_mpp


TEMPLATE = Path(__file__).resolve().parents[1] / "template.mpp"


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_export_and_read_back(tmp_path):
    plan = Plan(title="Round trip", start="2026-11-16", tasks=[
        Task(1, "Phase"), Task(2, "Design", parent=1, duration=2),
        Task(3, "Gate", parent=1, duration=0, predecessors=[2]),
        Task(4, "Build", resource="Ada", duration=2, predecessors=[3]),
    ])
    output = tmp_path / "output.mpp"
    before = TEMPLATE.read_bytes()
    export_mpp(plan, TEMPLATE, output)
    imported, notes = import_mpp(output)
    assert [(t.uid, t.name, t.parent) for t in imported.tasks] == [
        (1, "Phase", 0), (2, "Design", 1), (3, "Gate", 1), (4, "Build", 0)]
    assert imported.tasks[2].predecessors == [2]
    assert imported.resources[0].name == "Ada"
    assert imported.tasks[3].assignments == [ResourceAssignment(imported.resources[0].uid, 100)]
    assert schedule(imported)[3].duration == 0
    assert not notes
    assert TEMPLATE.read_bytes() == before
    with pytest.raises(ValueError, match="overwrit"):
        export_mpp(plan, TEMPLATE, TEMPLATE)
    with pytest.raises(ValueError, match="overwrite"):
        export_mpp(plan, TEMPLATE, output)


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_mpp_display_ids_follow_order_while_stable_uids_and_links_survive(tmp_path):
    from pymppwriter import blocks
    from pymppwriter.reader import _Klass, _Reader
    from pymppwriter.writer import NATIVE, TASK_META_SIZE, TASK_META2_SIZE
    from onepyproject.report import make_report

    plan = Plan(start="2026-11-16", tasks=[Task(30, "A"), Task(90, "Inserted"),
                    Task(10, "B", predecessors=["90SS+2h"])])
    output = tmp_path / "reordered.mpp"
    export_mpp(plan, TEMPLATE, output)
    reader = _Reader(str(output))
    task_records = _Klass(reader, "   114/TBkndTask", TASK_META_SIZE, TASK_META2_SIZE, blocks.PROPS_TASK_FIELD_MAP)
    ids = []
    for index, record in enumerate(task_records.recs):
        if len(record) <= 100:
            continue
        uid = task_records.value(index, NATIVE["UNIQUE_ID"], "<I")
        if uid:
            ids.append((uid, task_records.value(index, NATIVE["ID"], "<I")))
    assert ids == [(30, 1), (90, 2), (10, 3)]
    imported, _ = import_mpp(output)
    assert [task.uid for task in imported.tasks] == [30, 90, 10]
    link = parse_dependency(imported.tasks[-1].predecessors[0])
    assert (link.uid, link.kind, link.lag, link.unit) == (90, "SS", 0.25, "d")
    assert make_report(imported).rows[-1].predecessors == "2SS+0.25d"


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_import_existing_template():
    plan, _ = import_mpp(TEMPLATE)
    assert plan.tasks
    schedule(plan)


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_estimated_duration_mpp_roundtrip(tmp_path):
    plan = Plan(start="2026-11-16", tasks=[
        Task(1, "Summary"), Task(2, "Estimate", parent=1, duration=2, estimated=True),
        Task(3, "Confirmed"),
    ])
    output = tmp_path / "estimated.mpp"
    export_mpp(plan, TEMPLATE, output)
    imported, _ = import_mpp(output)
    assert [task.estimated for task in imported.tasks] == [True, True, False]


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_percent_complete_mpp_roundtrip(tmp_path):
    plan = Plan(start="2026-11-16", tasks=[Task(1, "Phase"),
        Task(2, "Partial", parent=1, duration=2, percent_complete=35),
        Task(3, "Complete", parent=1, duration=1, percent_complete=100)])
    output = tmp_path / "progress.mpp"
    export_mpp(plan, TEMPLATE, output)
    imported, notes = import_mpp(output)
    assert [task.percent_complete for task in imported.tasks] == [57, 35, 100]
    # Project holds a started task at its actual start, so the importer fixes both and says so
    assert [task.start_mode for task in imported.tasks] == ["no_earlier", "fixed", "fixed"]
    assert notes == ["Tasks fixed at their file dates because Project records them as 2 started: 2. "
                     "Predecessors do not move them"]


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_start_rules_survive_an_mpp_round_trip(tmp_path):
    plan = Plan(start="2026-11-16", tasks=[
        Task(1, "Lead", duration=2),
        Task(2, "Pinned before its link", duration=1, predecessors=[1], earliest="2026-11-16", start_mode="fixed"),
        Task(3, "Pinned after its link", duration=1, predecessors=[1], earliest="2026-11-23", start_mode="fixed"),
        Task(4, "Floor", duration=1, predecessors=[1], earliest="2026-11-25"),
        Task(5, "Link driven", duration=1, predecessors=[1])])
    expected = schedule(plan)
    output = tmp_path / "start-rules.mpp"
    export_mpp(plan, TEMPLATE, output)
    imported, _ = import_mpp(output)
    by_name = {task.name: task for task in imported.tasks}
    # A fixed task is written as Must Start On and comes back fixed; a floor comes back as a floor because
    # its date is later than its links give; a task its links already place carries no rule.
    assert [(name, task.start_mode, task.earliest) for name, task in by_name.items()] == [
        ("Lead", "no_earlier", ""),
        ("Pinned before its link", "fixed", "2026-11-16"),
        ("Pinned after its link", "fixed", "2026-11-23"),
        ("Floor", "no_earlier", "2026-11-25"),
        ("Link driven", "no_earlier", "")]
    dates = schedule(imported)
    assert {task.name: dates[task.uid].start for task in imported.tasks} == {
        task.name: expected[task.uid].start for task in plan.tasks}


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_custom_summary_color_is_reported_as_not_supported_in_mpp(tmp_path):
    plan = Plan(start="2026-11-16", tasks=[Task(1, "Phase", summary_color="#f08040"),
                                            Task(2, "Child", parent=1)])
    notes = export_mpp(plan, TEMPLATE, tmp_path / "custom-color.mpp")
    assert notes == ["Task 1: custom collapsed-summary color is not preserved in MPP"]


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_mpp_multiple_resources_and_unassigned_resource_roundtrip(tmp_path):
    plan = Plan(start="2026-11-16", resources=[Resource(1, "Ada", 75), Resource(2, "Ben", 150), Resource(7, "Unassigned", 0)], tasks=[
        Task(1, "Team task", duration=3,
             assignments=[ResourceAssignment(1, 50), ResourceAssignment(2, 125)]),
    ])
    output = tmp_path / "team.mpp"
    export_mpp(plan, TEMPLATE, output)
    imported, notes = import_mpp(output)
    assert [(r.uid, r.name, r.max_units) for r in imported.resources] == [(1, "Ada", 75), (2, "Ben", 150), (7, "Unassigned", 0)]
    assert imported.tasks[0].assignments == [ResourceAssignment(1, 50), ResourceAssignment(2, 125)]
    assert not notes


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_mpp_export_without_resources_does_not_keep_template_resources(tmp_path):
    plan = Plan(start="2026-11-16", tasks=[Task(1, "Unassigned")])
    output = tmp_path / "no-resources.mpp"
    export_mpp(plan, TEMPLATE, output)
    imported, notes = import_mpp(output)
    assert imported.resources == []
    assert imported.tasks[0].assignments == []
    assert not notes


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_mpp_fixed_work_and_multiple_assignments_roundtrip(tmp_path):
    plan = Plan(start="2026-11-16", resources=[Resource(1, "Ada"), Resource(2, "Ben")], tasks=[
        Task(1, "Effort", mode="work", work=80,
             assignments=[ResourceAssignment(1, 100), ResourceAssignment(2, 100)]),
    ])
    output = tmp_path / "effort.mpp"
    export_mpp(plan, TEMPLATE, output)
    imported, notes = import_mpp(output)
    assert imported.tasks[0].mode == "work"
    assert imported.tasks[0].work == 80
    assert len(imported.tasks[0].assignments) == 2
    assert schedule(imported)[1].duration == 5
    assert not notes


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_zero_allocation_from_mpp_is_not_silently_made_full_time(tmp_path, monkeypatch):
    import onepyproject.mpp as mpp
    plan = Plan(resources=[Resource(1, "Ada")], tasks=[
        Task(1, "Task", assignments=[ResourceAssignment(1, 100)]),
    ])
    output = tmp_path / "source.mpp"
    export_mpp(plan, TEMPLATE, output)
    read_values = mpp._mpp_resource_values
    def zero_allocation(path):
        capacities, allocations = read_values(path)
        return capacities, [(task_uid, resource_uid, 0) for task_uid, resource_uid, _ in allocations]
    monkeypatch.setattr(mpp, "_mpp_resource_values", zero_allocation)
    imported, notes = import_mpp(output)
    assert not imported.tasks[0].assignments
    assert any("0% allocation" in note for note in notes)


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_real_zero_allocation_in_mpp_is_reported(tmp_path):
    from pymppwriter import Assignment, MppWriter, Project, Resource as MppResource, Task as MppTask
    from onepyproject.mpp_template import strip_template_resources
    output = tmp_path / "zero.mpp"
    project = Project("Zero", datetime(2026, 11, 16, 8),
                      tasks=[MppTask(1, "Task", datetime(2026, 11, 16, 8), datetime(2026, 11, 16, 17))],
                      resources=[MppResource(1, "Ada")], assignments=[Assignment(1, 1, 0)])
    writer = MppWriter(str(TEMPLATE))
    strip_template_resources(writer)
    writer.write(project, str(output))
    imported, notes = import_mpp(output)
    assert not imported.tasks[0].assignments
    assert any("0% allocation" in note for note in notes)


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_duplicate_mpp_resource_names_are_distinguished_and_reported(tmp_path):
    from pymppwriter import Assignment, MppWriter, Project, Resource as MppResource, Task as MppTask
    from onepyproject.mpp_template import strip_template_resources
    output = tmp_path / "duplicate-names.mpp"
    project = Project("Duplicate names", datetime(2026, 11, 16, 8),
                      tasks=[MppTask(1, "Task", datetime(2026, 11, 16, 8), datetime(2026, 11, 16, 17))],
                      resources=[MppResource(1, "Ada"), MppResource(2, "Ada")],
                      assignments=[Assignment(1, 1, 0.5), Assignment(1, 2, 1)])
    writer = MppWriter(str(TEMPLATE))
    strip_template_resources(writer)
    writer.write(project, str(output))
    imported, notes = import_mpp(output)
    assert [(resource.uid, resource.name) for resource in imported.resources] == [(1, "Ada"), (2, "Ada (ID 2)")]
    assert imported.tasks[0].assignments == [ResourceAssignment(1, 50), ResourceAssignment(2, 100)]
    assert any("renamed duplicate" in note for note in notes)


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_summary_resource_assignment_is_not_silently_dropped_on_export(tmp_path):
    plan = Plan(resources=[Resource(1, "Ada")], tasks=[
        Task(1, "Summary", assignments=[ResourceAssignment(1)]), Task(2, "Child", parent=1),
    ])
    output = tmp_path / "summary.mpp"
    with pytest.raises(ValueError, match="summary resource assignments"):
        export_mpp(plan, TEMPLATE, output)
    assert not output.exists()


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_mpp_dependency_types_and_working_lag_roundtrip(tmp_path):
    plan = Plan(start="2026-11-16", tasks=[
        Task(1, "A", duration=4),
        Task(2, "FS", predecessors=["1FS-1d"]),
        Task(3, "SS", predecessors=["1SS+2h"]),
        Task(4, "FF", predecessors=["1FF-0.5d"]),
        Task(5, "SF", predecessors=["1SF+1w"]),
        Task(6, "Fraction", predecessors=["1FS+0.1m"]),
    ])
    output = tmp_path / "dependencies.mpp"
    export_mpp(plan, TEMPLATE, output)
    imported, notes = import_mpp(output)
    links = [parse_dependency(task.predecessors[0]) for task in imported.tasks[1:]]
    assert [(link.kind, link.lag) for link in links] == [
        ("FS", -1), ("SS", 0.25), ("FF", -0.5), ("SF", 5), ("FS", pytest.approx(1 / 4800)),
    ]
    assert not notes


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
@pytest.mark.parametrize("link", ["1FS+2ed", "1FS+50%"])
def test_mpp_export_rejects_unrepresentable_lag(tmp_path, link):
    plan = Plan(tasks=[Task(1, "A"), Task(2, "B", predecessors=[link])])
    output = tmp_path / "unsafe.mpp"
    with pytest.raises(ValueError, match="cannot preserve elapsed or percentage"):
        export_mpp(plan, TEMPLATE, output)
    assert not output.exists()


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_mpp_import_does_not_guess_unknown_lag_units(tmp_path, monkeypatch):
    import onepyproject.mpp as mpp
    plan = Plan(tasks=[Task(1, "A"), Task(2, "B", predecessors=["1SS+1d"])])
    output = tmp_path / "source.mpp"
    export_mpp(plan, TEMPLATE, output)
    real_records = mpp._mpp_relation_records
    def unknown_units(path):
        return [(pred, succ, kind, 8, lag) for pred, succ, kind, _, lag in real_records(path)]
    monkeypatch.setattr(mpp, "_mpp_relation_records", unknown_units)
    imported, notes = import_mpp(output)
    assert not imported.tasks[1].predecessors
    assert any("unsupported MPP" in note for note in notes)


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_fixed_units_and_summary_links_survive_an_export_and_import(tmp_path):
    plan = Plan(title="Round trip", start="2026-11-16", resources=[Resource(1, "Ada", 100)], tasks=[
        Task(1, "Lead in", duration=3, mode="units", assignments=[ResourceAssignment(1, 100)]),
        Task(2, "Phase", predecessors=[1]),
        Task(3, "First", parent=2, duration=2),
        Task(4, "Second", parent=2, duration=1, mode="work", work=8, assignments=[ResourceAssignment(1, 100)],
             predecessors=[3]),
        Task(5, "Follow-on", duration=1, predecessors=["2FS+1d"]),
    ])
    output = tmp_path / "units-and-summary-links.mpp"
    export_mpp(plan, TEMPLATE, output)
    imported, notes = import_mpp(output)
    assert [(t.uid, t.mode) for t in imported.tasks] == [(1, "units"), (2, "duration"), (3, "duration"),
                                                         (4, "work"), (5, "duration")]
    assert imported.tasks[1].predecessors == [1]                 # a link *to* a summary
    assert [parse_dependency(p).uid for p in imported.tasks[4].predecessors] == [2]   # a link *from* a summary
    assert not [note for note in notes if "omitted" in note or "fixed-units" in note]


def test_export_hands_pymppwriter_summary_links_and_task_types(tmp_path, monkeypatch):
    """No template needed: capture what export gives pymppwriter and run its own validation."""
    from onepyproject import mpp
    from pymppwriter.writer import effective_schedule, validate

    captured = {}

    class CapturingWriter:
        def __init__(self, template):
            pass

        def write(self, project, destination):
            validate(project)                  # structure, outline and cycle checks
            effective_schedule(project)        # summary rows are rolled up, so summary links resolve
            captured["project"] = project
            Path(destination).write_bytes(b"")

    monkeypatch.setattr(mpp, "MppWriter", CapturingWriter)
    monkeypatch.setattr(mpp, "strip_template_resources", lambda writer: None)
    template = tmp_path / "empty-template.mpp"
    template.write_bytes(b"")
    plan = Plan(start="2026-11-16", resources=[Resource(1, "Ada", 100)], tasks=[
        Task(1, "Lead in", duration=3, mode="units", assignments=[ResourceAssignment(1, 100)]),
        Task(2, "Phase", predecessors=["1"]),
        Task(3, "First", parent=2, duration=2),
        Task(4, "Second", parent=2, duration=1, mode="work", work=8, assignments=[ResourceAssignment(1, 100)],
             predecessors=[3]),
        Task(5, "Follow-on", duration=1, predecessors=["2FS+1d"]),
    ])
    export_mpp(plan, template, tmp_path / "out.mpp")
    project = captured["project"]
    assert [t.task_type for t in project.tasks] == ["fixed_units", "fixed_duration", "fixed_duration",
                                                    "fixed_work", "fixed_duration"]
    assert [(r.pred_uid, r.succ_uid, r.type, r.lag_days) for r in project.relations] == [
        (1, 2, "FS", 0.0), (3, 4, "FS", 0.0), (2, 5, "FS", 1.0)]


def test_import_keeps_summary_links_maps_fixed_units_and_reports_what_it_drops(tmp_path, monkeypatch):
    """Importer mapping on stand-in source data, so no MPP file is needed."""
    from types import SimpleNamespace as NS
    from onepyproject import mpp

    def task(uid, name, start, days, parent=0):
        return NS(uid=uid, name=name, parent_uid=parent, start=datetime.fromisoformat(start),
                  duration_days=days, estimated=False, percent_complete=0)

    def link(pred, succ, kind):
        return NS(pred_uid=pred, succ_uid=succ, type=kind)

    source = NS(title="Stand-in", start=datetime(2026, 11, 16, 8), resources=[], assignments=[], tasks=[
        task(1, "Lead in", "2026-11-16", 3), task(2, "Phase", "2026-11-17", 1),
        task(3, "First", "2026-11-17", 1, parent=2), task(4, "Second", "2026-11-20", 1, parent=2),
        task(5, "After", "2026-11-24", 1), task(6, "Side", "2026-11-16", 1),
    ], relations=[link(1, 2, "FS"), link(2, 5, "FS"), link(6, 2, "FF"), link(99, 3, "FS")])
    records = [(1, 2, 1, 7, 0), (2, 5, 1, 7, 0), (6, 2, 0, 7, 0), (99, 3, 1, 7, 0)]
    types = {1: (0, 24.0), 3: (0, 8.0), 4: (0, 8.0), 5: (1, 8.0), 6: (0, 8.0)}
    monkeypatch.setattr(mpp, "read_project", lambda path: source)
    monkeypatch.setattr(mpp, "_mpp_resource_values", lambda path: ({}, []))
    monkeypatch.setattr(mpp, "_mpp_task_work", lambda path: types)
    monkeypatch.setattr(mpp, "_mpp_relation_records", lambda path: records)

    plan, notes = import_mpp(tmp_path / "ignored.mpp")
    assert [(t.uid, t.mode) for t in plan.tasks] == [(1, "units"), (2, "duration"), (3, "units"),
                                                     (4, "units"), (5, "duration"), (6, "units")]
    assert plan.tasks[1].predecessors == [1]          # link to a summary is kept
    assert plan.tasks[4].predecessors == [2]          # link from a summary is kept
    assert notes == [
        "Task 2: omitted FF dependency on task 6; a summary task supports only FS and SS links",
        "Task 3: omitted dependency on missing task UID 99",
        "Tasks also fixed at their file dates because the file starts them before their predecessors "
        "allow: 1; the first is Task 3 (2026-11-17)",
        "Fixed tasks that start before their predecessors finish, as in the file: 1. "
        "The links are kept; the first is Task 3",
    ]
    # The task the file starts before its summary's predecessor is held where the file has it.
    assert (plan.tasks[2].start_mode, plan.tasks[2].earliest) == ("fixed", "2026-11-17")
    assert schedule(plan)[3].start.date().isoformat() == "2026-11-17"


def test_import_starts_the_plan_at_the_earliest_task_when_the_recorded_start_is_later(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from onepyproject import mpp

    def task(uid, start, days=2):
        return NS(uid=uid, name=f"Task {uid}", parent_uid=0, start=datetime.fromisoformat(start),
                  duration_days=days, estimated=False, percent_complete=0)

    source = NS(title="Late start", start=datetime(2026, 6, 2, 8), resources=[], assignments=[],
                tasks=[task(1, "2025-11-03"), task(2, "2026-02-05")], relations=[])
    monkeypatch.setattr(mpp, "read_project", lambda path: source)
    monkeypatch.setattr(mpp, "_mpp_resource_values", lambda path: ({}, []))
    monkeypatch.setattr(mpp, "_mpp_task_work", lambda path: {})
    monkeypatch.setattr(mpp, "_mpp_relation_records", lambda path: [])

    plan, notes = import_mpp(tmp_path / "ignored.mpp")
    assert plan.start == "2025-11-03"
    assert notes == ["Project start moved from 2026-06-02 to 2025-11-03, the earliest task start in the file"]
    items = schedule(plan)
    assert items[1].start.date().isoformat() == "2025-11-03" and items[2].start.date().isoformat() == "2026-02-05"

    source.start = datetime(2025, 10, 1, 8)               # an earlier recorded start is kept as it is
    plan, notes = import_mpp(tmp_path / "ignored.mpp")
    assert plan.start == "2025-10-01" and not notes


def _stand_in(monkeypatch, tasks, relations, start=datetime(2026, 11, 16, 8)):
    """Patch the reader so importer decisions can be tested without an MPP file."""
    from types import SimpleNamespace as NS
    from onepyproject import mpp

    source = NS(title="Stand-in", start=start, resources=[], assignments=[], tasks=tasks, relations=relations)
    monkeypatch.setattr(mpp, "read_project", lambda path: source)
    monkeypatch.setattr(mpp, "_mpp_resource_values", lambda path: ({}, []))
    monkeypatch.setattr(mpp, "_mpp_task_work", lambda path: {})
    monkeypatch.setattr(mpp, "_mpp_relation_records",
                        lambda path: [(r.pred_uid, r.succ_uid, 1, 7, 0) for r in relations])
    return source


def _source_task(uid, start, days=1, percent=0, manual=False, constraint=None):
    from types import SimpleNamespace as NS
    return NS(uid=uid, name=f"Task {uid}", parent_uid=0, start=datetime.fromisoformat(start), duration_days=days,
              estimated=False, percent_complete=percent, manual=manual, constraint=constraint)


def test_import_gives_each_task_the_start_hint_the_file_needs(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from onepyproject.model import start_conflicts

    links = [NS(pred_uid=1, succ_uid=uid, type="FS") for uid in (2, 3, 4, 7)]
    _stand_in(monkeypatch, [
        _source_task(1, "2026-11-16", 3),                         # no links: starts on the project start
        _source_task(2, "2026-11-19"),                            # the link gives exactly this date
        _source_task(3, "2026-11-26"),                            # later than the link gives: a floor
        _source_task(4, "2026-11-17", percent=40),                # started before its predecessor finished
        _source_task(5, "2026-11-30", manual=True),               # manually scheduled
        _source_task(6, "2026-12-01", constraint="MSO"),          # Must Start On
        _source_task(7, "2026-11-17"),                            # the file breaks this link, no recorded cause
        _source_task(8, "2026-11-16", constraint="SNET"),         # a constraint that changes nothing
    ], links)

    plan, notes = import_mpp(tmp_path / "ignored.mpp")
    hints = {task.uid: (task.earliest, task.start_mode) for task in plan.tasks}
    assert hints == {
        1: ("", "no_earlier"), 2: ("", "no_earlier"), 3: ("2026-11-26", "no_earlier"),
        4: ("2026-11-17", "fixed"), 5: ("2026-11-30", "fixed"), 6: ("2026-12-01", "fixed"),
        7: ("2026-11-17", "fixed"), 8: ("", "no_earlier"),
    }
    assert notes == [
        "Tasks fixed at their file dates because Project records them as 1 started, 1 manually scheduled, "
        "1 Must Start On: 3. Predecessors do not move them",
        "Tasks also fixed at their file dates because the file starts them before their predecessors "
        "allow: 1; the first is Task 7 (2026-11-17)",
        "Fixed tasks that start before their predecessors finish, as in the file: 2. "
        "The links are kept; the first is Task 4",
    ]
    assert sorted(start_conflicts(plan)) == [4, 7]
    # Every leaf lands on the date the file stored.
    assert {uid: item.start.date().isoformat() for uid, item in schedule(plan).items()} == {
        1: "2026-11-16", 2: "2026-11-19", 3: "2026-11-26", 4: "2026-11-17",
        5: "2026-11-30", 6: "2026-12-01", 7: "2026-11-17", 8: "2026-11-16"}


def test_imported_links_drive_tasks_that_have_no_hint(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    _stand_in(monkeypatch, [_source_task(1, "2026-11-16", 3), _source_task(2, "2026-11-19")],
              [NS(pred_uid=1, succ_uid=2, type="FS")])
    plan, notes = import_mpp(tmp_path / "ignored.mpp")
    assert not notes and plan.tasks[1].earliest == ""
    plan.tasks[0].duration = 5                                    # lengthen the lead-in
    assert schedule(plan)[2].start.date().isoformat() == "2026-11-23"   # the follower moves with it


def test_a_summary_task_never_gets_a_start_hint(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    phase = _source_task(1, "2026-11-16", 2, percent=50, manual=True)
    child = _source_task(2, "2026-11-16", 2, percent=50)
    child.parent_uid = 1
    _stand_in(monkeypatch, [phase, child], [])
    plan, _ = import_mpp(tmp_path / "ignored.mpp")
    assert (plan.tasks[0].earliest, plan.tasks[0].start_mode) == ("", "no_earlier")
    assert (plan.tasks[1].earliest, plan.tasks[1].start_mode) == ("2026-11-16", "fixed")


def test_export_writes_fixed_starts_as_constraints_and_drops_their_false_warnings(tmp_path, monkeypatch):
    """No template needed: capture what export hands pymppwriter."""
    import warnings
    from onepyproject import mpp
    from pymppwriter.writer import ScheduleWarning

    captured = {}

    class CapturingWriter:
        def __init__(self, template):
            pass

        def write(self, project, destination):
            captured["project"] = project
            # What pymppwriter says about a task that starts before its links allow.
            warnings.warn("task 2 'Pinned' starts 2026-11-17 08:00 but its predecessors put it at "
                          "2026-11-19 08:00; Project will move it", ScheduleWarning)
            warnings.warn("task 3 'Floor' starts 2026-11-17 08:00 but its predecessors put it at "
                          "2026-11-19 08:00; Project will move it", ScheduleWarning)
            Path(destination).write_bytes(b"")

    monkeypatch.setattr(mpp, "MppWriter", CapturingWriter)
    monkeypatch.setattr(mpp, "strip_template_resources", lambda writer: None)
    template = tmp_path / "empty-template.mpp"
    template.write_bytes(b"")
    plan = Plan(start="2026-11-16", tasks=[
        Task(1, "Lead in", duration=3),
        Task(2, "Pinned", duration=1, predecessors=[1], earliest="2026-11-17", start_mode="fixed"),
        Task(3, "Floor", duration=1, predecessors=[1], earliest="2026-11-17"),
        Task(4, "Started", duration=2, predecessors=[1], earliest="2026-11-17", start_mode="fixed",
             percent_complete=50),
        Task(5, "Free", duration=1)])
    notes = export_mpp(plan, template, tmp_path / "out.mpp")
    by_uid = {t.uid: t for t in captured["project"].tasks}
    assert (by_uid[2].constraint, by_uid[2].constraint_date) == ("MSO", datetime(2026, 11, 17, 8))
    # Progress is written by pymppwriter as an actual start, so no constraint is added.
    assert by_uid[4].constraint is None and by_uid[4].percent_complete == 50
    assert by_uid[1].constraint is None and by_uid[3].constraint is None and by_uid[5].constraint is None
    assert notes == [
        "Fixed tasks that start before their predecessors finish: 2. The links are written unchanged and the "
        "fixed dates are kept",
        "task 3 'Floor' starts 2026-11-17 08:00 but its predecessors put it at 2026-11-19 08:00; "
        "Project will move it",
    ]


@pytest.mark.parametrize("unit, raw, expected", [
    (3, 150, "1FS+15m"),
    (5, 6000, "1FS+10h"),
    (7, -4800, "1FS-1d"),
    (9, -216000, "1FS-9w"),            # a nine-week lead, as in a real file
    (9, -48000, "1FS-2w"),
    (11, 96000, "1FS+1mo"),
])
def test_import_keeps_working_time_lags_in_the_unit_the_file_shows(tmp_path, monkeypatch, unit, raw, expected):
    from types import SimpleNamespace as NS
    from onepyproject import mpp
    from onepyproject.dependencies import lag_minutes

    _stand_in(monkeypatch, [_source_task(1, "2026-11-16"), _source_task(2, "2026-11-17")],
              [NS(pred_uid=1, succ_uid=2, type="FS")])
    monkeypatch.setattr(mpp, "_mpp_relation_records", lambda path: [(1, 2, 1, unit, raw)])
    plan, notes = import_mpp(tmp_path / "ignored.mpp")
    assert plan.tasks[1].predecessors == [expected]
    assert not [note for note in notes if "omitted" in note]
    # The scheduler must read the lag as the same working minutes the file stores (tenths of a minute).
    minutes, elapsed = lag_minutes(parse_dependency(plan.tasks[1].predecessors[0]), 0)
    assert minutes == pytest.approx(raw / 10) and not elapsed


@pytest.mark.parametrize("unit", [4, 6, 8, 10, 12, 19, 20])
def test_import_still_omits_lags_that_are_not_working_time(tmp_path, monkeypatch, unit):
    from types import SimpleNamespace as NS
    from onepyproject import mpp

    _stand_in(monkeypatch, [_source_task(1, "2026-11-16"), _source_task(2, "2026-11-17")],
              [NS(pred_uid=1, succ_uid=2, type="FS")])
    monkeypatch.setattr(mpp, "_mpp_relation_records", lambda path: [(1, 2, 1, unit, 4800)])
    plan, notes = import_mpp(tmp_path / "ignored.mpp")
    assert not plan.tasks[1].predecessors
    assert f"Task 2: omitted dependency 1 with unsupported MPP link type or lag unit ({unit})" in notes


def test_import_accepts_a_zero_lag_whatever_its_unit(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from onepyproject import mpp

    _stand_in(monkeypatch, [_source_task(1, "2026-11-16"), _source_task(2, "2026-11-17")],
              [NS(pred_uid=1, succ_uid=2, type="FS")])
    monkeypatch.setattr(mpp, "_mpp_relation_records", lambda path: [(1, 2, 1, 8, 0)])
    plan, notes = import_mpp(tmp_path / "ignored.mpp")
    assert plan.tasks[1].predecessors == [1]
    assert not [note for note in notes if "omitted" in note]


def test_import_merges_identical_duplicate_links(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from onepyproject import mpp

    # A real file held two identical finish-to-start records between the same two tasks.
    relations = [NS(pred_uid=1, succ_uid=2, type="FS"), NS(pred_uid=1, succ_uid=2, type="FS")]
    _stand_in(monkeypatch, [_source_task(1, "2026-11-16"), _source_task(2, "2026-11-17")], relations)
    monkeypatch.setattr(mpp, "_mpp_relation_records", lambda path: [(1, 2, 1, 7, 0), (1, 2, 1, 7, 0)])
    plan, notes = import_mpp(tmp_path / "ignored.mpp")
    assert plan.tasks[1].predecessors == [1]
    assert "Task 2: merged duplicate dependency on 1" in notes
    assert not [note for note in notes if "omitted" in note]


def test_import_keeps_the_first_of_differing_duplicate_links_and_says_so(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from onepyproject import mpp

    relations = [NS(pred_uid=1, succ_uid=2, type="FS"), NS(pred_uid=1, succ_uid=2, type="SS")]
    _stand_in(monkeypatch, [_source_task(1, "2026-11-16"), _source_task(2, "2026-11-17")], relations)
    monkeypatch.setattr(mpp, "_mpp_relation_records", lambda path: [(1, 2, 1, 7, 0), (1, 2, 2, 7, 9600)])
    plan, notes = import_mpp(tmp_path / "ignored.mpp")
    assert plan.tasks[1].predecessors == [1]
    assert "Task 2: omitted a second dependency on 1 (SS+2d); kept FS" in notes


def test_import_keeps_links_to_different_predecessors(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from onepyproject import mpp

    relations = [NS(pred_uid=1, succ_uid=3, type="FS"), NS(pred_uid=2, succ_uid=3, type="FS")]
    _stand_in(monkeypatch, [_source_task(1, "2026-11-16"), _source_task(2, "2026-11-16"),
                            _source_task(3, "2026-11-17")], relations)
    monkeypatch.setattr(mpp, "_mpp_relation_records", lambda path: [(1, 3, 1, 7, 0), (2, 3, 1, 7, 0)])
    plan, notes = import_mpp(tmp_path / "ignored.mpp")
    assert plan.tasks[2].predecessors == [1, 2]
    assert not [note for note in notes if "duplicate" in note]


# ---- project calendar: holidays and working days ----------------------------------------------

# Real 92-byte exception records from a Project-written file: a four-day holiday named "Easter"
# (without its name bytes), and a Saturday worked 08:00-12:00 and 13:00-17:00 (2026-10-31, no name).
REAL_HOLIDAY_RECORD = bytes.fromhex(
    "4a3c4d3c04000000" + "00" * 64 + "01000000ffffffff01000000ffffffff0e000000")
REAL_WORKING_RECORD = bytes.fromhex(
    "1d3d1d3d010000000000000000000200c0120000c012781e0000000000000000600900006009000000000000"
    "000000000000000060090000c01200000000000000000000000000000100000000000000040000000000000000000000")


def _exception(first, last, name="", shifts=(), recurrence=0, occurrences=1, days=None):
    """An exception record in the layout of the real ones above; ``first`` and ``last`` are dates."""
    import struct
    from datetime import date
    epoch = date(1983, 12, 31)
    record = bytearray(92)
    first_day, last_day = (first - epoch).days, (last - epoch).days
    struct.pack_into("<HHH", record, 0, first_day, last_day, last_day - first_day + 1 if days is None else days)
    struct.pack_into("<H", record, 6, recurrence)
    struct.pack_into("<H", record, 14, len(shifts))
    for i, (start, finish) in enumerate(shifts):          # tenths of a minute
        struct.pack_into("<H", record, 20 + 2 * i, start)
        struct.pack_into("<I", record, 32 + 4 * i, finish - start)
    struct.pack_into("<I", record, 72, occurrences)
    encoded = (name + "\0").encode("utf-16-le") if name else b""
    struct.pack_into("<I", record, 88, len(encoded))
    return bytes(record) + encoded + b"\0" * ((-len(encoded)) % 4)


def _calendar_blob(exceptions=(), week=None):
    import struct
    blob = bytearray(week if week is not None else b"\x01\x00" + b"\0" * 58 + (b"\x01\x00" + b"\0" * 58) * 6)
    if exceptions:
        blob += struct.pack("<I", len(exceptions)) + b"".join(exceptions) + b"\0\0\0\0"
    return bytes(blob)


def _read_calendar(blob):
    from onepyproject import mpp
    result = mpp.ProjectCalendar(name="Standard")
    mpp._read_exceptions(blob, result)
    return result


def test_real_exception_records_are_read():
    from datetime import date
    from onepyproject.workcalendar import Period

    result = _read_calendar(_calendar_blob([REAL_HOLIDAY_RECORD + "Easter\0".encode("utf-16-le") + b"\0\0",
                                            REAL_WORKING_RECORD]))
    assert result.holidays == [Period(date(2026, 4, 3), date(2026, 4, 6), "Easter")]
    assert result.working_days == [Period(date(2026, 10, 31), date(2026, 10, 31), "")]
    assert not result.notes


def test_pymppwriter_exceptions_are_read_back_as_named_holiday_periods():
    from datetime import date
    from pymppwriter import blocks
    from onepyproject.workcalendar import Period

    days = [(blocks.CAL_DAY_DEFAULT, ())] * 7
    blob = blocks.build_calendar_data(days, [(date(2026, 12, 21), date(2027, 1, 1), "Christmas shutdown"),
                                             (date(2026, 12, 25 + 3), date(2026, 12, 28), "")])
    result = _read_calendar(blob)
    assert result.holidays == [Period(date(2026, 12, 21), date(2027, 1, 1), "Christmas shutdown")]  # merged
    assert not result.notes


def test_overlapping_exceptions_merge_and_a_name_is_made_storable():
    from datetime import date

    result = _read_calendar(_calendar_blob([
        _exception(date(2026, 12, 21), date(2026, 12, 25), "Christmas, part 1"),
        _exception(date(2026, 12, 24), date(2026, 12, 31), "Part 2"),
        _exception(date(2027, 1, 4), date(2027, 1, 4), "..Odd"),
    ]))
    assert [(p.start, p.finish, p.name) for p in result.holidays] == [
        (date(2026, 12, 21), date(2026, 12, 31), "Christmas part 1"), (date(2027, 1, 4), date(2027, 1, 4), "Odd")]


def test_exceptions_with_other_hours_or_a_recurrence_are_reported_not_imported():
    from datetime import date

    result = _read_calendar(_calendar_blob([
        _exception(date(2026, 12, 24), date(2026, 12, 24), "Half day", shifts=[(4800, 7200)]),
        _exception(date(2026, 12, 25), date(2026, 12, 25), "Yearly", recurrence=3),
        _exception(date(2026, 12, 26), date(2026, 12, 26), "Twice", occurrences=2),
        _exception(date(2026, 12, 27), date(2026, 12, 30), "Bad count", days=9),
        _exception(date(2026, 12, 31), date(2026, 12, 31), "Real holiday"),
    ]))
    assert [p.name for p in result.holidays] == ["Real holiday"] and not result.working_days
    assert result.notes == [
        "1 exception with working hours other than 08:00-12:00 and 13:00-17:00 is not imported, so those days are "
        "worked as normal days; the first is 2026-12-24 Half day",
        "3 exceptions that cannot be represented (recurring, overlapping or very long) are not imported; "
        "the first is 2026-12-25 Yearly",
    ]


def test_a_working_day_that_clashes_with_a_holiday_is_not_imported():
    from datetime import date

    standard = [(4800, 7200), (7800, 10200)]
    result = _read_calendar(_calendar_blob([
        _exception(date(2026, 10, 30), date(2026, 11, 2), "Bridge"),
        _exception(date(2026, 10, 31), date(2026, 10, 31), "Catch-up", shifts=standard),
    ]))
    assert [p.name for p in result.holidays] == ["Bridge"] and not result.working_days
    assert "1 exception that cannot be represented" in result.notes[0] and "2026-10-31" in result.notes[0]


def test_a_truncated_exception_list_is_an_error_the_importer_turns_into_a_note():
    from datetime import date

    blob = _calendar_blob([_exception(date(2026, 12, 24), date(2026, 12, 24), "Cut")])[:-40]
    with pytest.raises(ValueError, match="past the end"):
        _read_calendar(blob)


@pytest.mark.parametrize("pattern, standard", [(None, True), ("explicit", True), ("saturday", False), ("short", False)])
def test_only_the_standard_week_is_recognised(pattern, standard):
    import struct
    from onepyproject import mpp

    def block(day_type, shifts=()):
        data = bytearray(60)
        struct.pack_into("<HH", data, 0, day_type, len(shifts))
        for i, (start, finish) in enumerate(shifts):
            struct.pack_into("<H", data, 8 + 2 * i, start)
            struct.pack_into("<I", data, 20 + 4 * i, finish - start)
        return bytes(data)

    full = [(4800, 7200), (7800, 10200)]
    weekdays = [block(0, full)] * 5
    week = {None: [block(1)] * 7,
            "explicit": [block(0)] + weekdays + [block(0)],
            "saturday": [block(0)] + weekdays + [block(0, full)],
            "short": [block(0)] + weekdays[:4] + [block(0, [(4800, 7200)])] + [block(0)]}[pattern]
    assert mpp._is_standard_week(b"".join(week)) is standard


@pytest.fixture(autouse=True)
def _stand_in_files_have_no_calendar(monkeypatch):
    """Importer tests that stub the readers name files that do not exist; give them an empty calendar."""
    from onepyproject import mpp

    real = mpp._mpp_calendar
    monkeypatch.setattr(mpp, "_mpp_calendar",
                        lambda path: real(path) if Path(path).exists() else mpp.ProjectCalendar(name="Standard"))


def _calendar(holidays=(), working=(), notes=()):
    from onepyproject import mpp
    from onepyproject.workcalendar import parse_period

    return mpp.ProjectCalendar(name="Standard", holidays=[parse_period(x) for x in holidays],
                               working_days=[parse_period(x) for x in working], notes=list(notes))


def test_import_sets_the_calendar_before_choosing_start_hints(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from onepyproject import mpp

    # Task 2 follows task 1 and the file has it on the Friday, after the Thursday holiday.
    _stand_in(monkeypatch, [_source_task(1, "2026-11-16", 3), _source_task(2, "2026-11-20")],
              [NS(pred_uid=1, succ_uid=2, type="FS")])
    monkeypatch.setattr(mpp, "_mpp_calendar", lambda path: _calendar(holidays=["2026-11-19 Day off"]))
    plan, notes = import_mpp(tmp_path / "ignored.mpp")
    assert plan.holidays == ["2026-11-19 Day off"] and plan.working_days == []
    assert notes == ["Imported 1 holiday period from the project calendar 'Standard'"]
    # The links alone give the file's date on this calendar, so the task needs no hint.
    assert (plan.tasks[1].start_mode, plan.tasks[1].earliest) == ("no_earlier", "")
    assert schedule(plan)[2].start.date().isoformat() == "2026-11-20"

    monkeypatch.setattr(mpp, "_mpp_calendar", lambda path: _calendar())
    plan, notes = import_mpp(tmp_path / "ignored.mpp")
    assert plan.tasks[1].earliest == "2026-11-20"        # without the holiday the file's date is a floor
    assert not notes


def test_import_keeps_a_task_on_a_working_saturday_where_the_file_has_it(tmp_path, monkeypatch):
    from onepyproject import mpp

    _stand_in(monkeypatch, [_source_task(1, "2026-10-30"), _source_task(2, "2026-10-31")], [],
              start=datetime(2026, 10, 26, 8))
    monkeypatch.setattr(mpp, "_mpp_calendar", lambda path: _calendar(working=["2026-10-31"]))
    plan, notes = import_mpp(tmp_path / "ignored.mpp")
    assert plan.working_days == ["2026-10-31"] and plan.holidays == []
    assert notes == ["Imported 1 working day from the project calendar 'Standard'"]
    assert schedule(plan)[2].start.date().isoformat() == "2026-10-31"

    monkeypatch.setattr(mpp, "_mpp_calendar", lambda path: _calendar())
    plan, notes = import_mpp(tmp_path / "ignored.mpp")
    assert any("Recalculation moves 1 task" in note and "2026-10-31 became 2026-11-02" in note for note in notes)


def test_import_counts_both_kinds_and_forwards_what_the_calendar_left_out(tmp_path, monkeypatch):
    from onepyproject import mpp

    _stand_in(monkeypatch, [_source_task(1, "2026-11-16")], [])
    monkeypatch.setattr(mpp, "_mpp_calendar", lambda path: _calendar(
        holidays=["2026-12-21..2027-01-01 Christmas", "2026-04-03"], working=["2026-10-31", "2026-11-07"],
        notes=["1 task uses a task calendar, which is not applied; they are scheduled on the project calendar"]))
    plan, notes = import_mpp(tmp_path / "ignored.mpp")
    assert plan.holidays == ["2026-12-21..2027-01-01 Christmas", "2026-04-03"]
    assert plan.working_days == ["2026-10-31", "2026-11-07"]
    assert notes[:2] == ["Imported 2 holiday periods and 2 working days from the project calendar 'Standard'",
                         "Project calendar: 1 task uses a task calendar, which is not applied; "
                         "they are scheduled on the project calendar"]


def test_an_unreadable_calendar_does_not_stop_the_import(tmp_path, monkeypatch):
    from onepyproject import mpp

    def broken(path):
        raise ValueError("the calendar exceptions run past the end of the data")

    _stand_in(monkeypatch, [_source_task(1, "2026-11-16")], [])
    monkeypatch.setattr(mpp, "_mpp_calendar", broken)
    plan, notes = import_mpp(tmp_path / "ignored.mpp")
    assert plan.holidays == [] and plan.working_days == [] and len(plan.tasks) == 1
    assert notes == ["Project calendar: the project calendar could not be read (the calendar exceptions run past "
                     "the end of the data), so no holidays or working days were imported"]


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_holiday_periods_and_names_survive_an_mpp_round_trip(tmp_path):
    plan = Plan(title="Calendar", start="2026-12-18",
                holidays=["2026-12-21..2027-01-01 Christmas shutdown", "2026-12-25 Duplicate", "2027-04-02..2027-04-05 Easter",
                          "2027-06-14"],
                tasks=[Task(1, "Before", duration=1), Task(2, "After", duration=2, predecessors=[1])])
    output = tmp_path / "calendar.mpp"
    notes = export_mpp(plan, TEMPLATE, output)
    assert not [note for note in notes if "Working days" in note]
    imported, import_notes = import_mpp(output)
    # The repeated 25 December joins the shutdown; every period keeps its name.
    assert imported.holidays == ["2026-12-21..2027-01-01 Christmas shutdown", "2027-04-02..2027-04-05 Easter",
                                 "2027-06-14"]
    assert imported.working_days == []
    assert "Imported 3 holiday periods from the project calendar 'Standard'" in import_notes
    before, after = schedule(plan), schedule(imported)
    assert [after[uid].start for uid in (1, 2)] == [before[uid].start for uid in (1, 2)]
    assert after[2].start.date().isoformat() == "2027-01-04" and after[2].finish.date().isoformat() == "2027-01-05"


@pytest.mark.skipif(not TEMPLATE.exists(), reason="User-supplied MPP template not available")
def test_export_reports_weekend_working_days_it_cannot_write(tmp_path):
    base = dict(title="Calendar", start="2026-10-26", tasks=[Task(1, "Work", duration=1)])
    weekday = export_mpp(Plan(**base, working_days=["2026-10-28"]), TEMPLATE, tmp_path / "weekday.mpp")
    assert not [note for note in weekday if "Working days" in note]       # a weekday is worked anyway
    notes = export_mpp(Plan(**base, working_days=["2026-10-28", "2026-10-31..2026-11-01"]), TEMPLATE,
                       tmp_path / "weekend.mpp")
    assert ("Working days on a weekend are not written to the MPP, so Project schedules them as non-working: 2; "
            "the first is 2026-10-31") in notes
    imported, _ = import_mpp(tmp_path / "weekend.mpp")
    assert imported.working_days == []
