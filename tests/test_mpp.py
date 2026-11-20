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
