"""Lossy MPP interchange. The OnePyProject plan is the editable source of truth."""
from __future__ import annotations

from collections import Counter
import copy
from datetime import datetime
import math
from pathlib import Path
import os
import re
import struct
import tempfile
import warnings

from pymppwriter import Assignment as MppAssignment
from pymppwriter import MppWriter, Project, Relation
from pymppwriter import Resource as MppResource
from pymppwriter import Task as MppTask
from pymppwriter import read_project

from .model import (Plan, Resource, ResourceAssignment, Task, completion_percentages, estimated_tasks,
                    schedule, start_conflicts)
from .dependencies import Dependency, format_dependency, lag_minutes, parse_dependency, task_numbers
from .resources import migrate_legacy_resources
from .mpp_template import strip_template_resources


IMPORT_WARNING = (
    "MPP import is experimental and lossy. The reader can skip unscheduled tasks and "
    "does not reliably read calendars, task types or all Project fields. This app "
    "recalculates using its own limited scheduler. Costs, custom fields, "
    "baselines, resource calendars, rates, timephased work, some lag units and other data may be lost. "
    "Keep the original MPP and verify the imported schedule."
)
EXPORT_TASK_TYPES = {"duration": "fixed_duration", "work": "fixed_work", "units": "fixed_units"}
EXPORT_WARNING = (
    "An empty MPP template is required to save this project as an MPP. "
    "Some metadata may be retained from the template in the saved file.\n\n"
    "MPP support is experimental. Save OnePyProject (.pxplan) to retain your work."
)


def _mpp_relation_records(path: str | Path) -> list[tuple[int, int, int, int, int]]:
    """Read lag unit codes which pymppwriter 0.4.3's public reader discards.

    The library is pinned; fail instead of guessing if its private structures change.
    """
    from pymppwriter import blocks as blocks
    from pymppwriter.reader import _Reader, declared_count
    reader = _Reader(str(path))
    fields, _ = reader._class_map(blocks.PROPS_RELATION_FIELD_MAP)
    old_layout = fields.get(9) is not None and fields[9].offset == 0
    base = "   114/TBkndCons"
    try:
        meta = blocks.parse_fixed_meta_auto(reader._get(base + "/FixedMeta"), 10)[2]
        records = blocks.split_fixed_data(reader._get(base + "/FixedData"), meta)
    except KeyError:
        return []
    result = []
    # Same live-record rule as the reader: entries past the declared count are stale.
    for record in records[:declared_count(reader.props, blocks.PROPS_REL_RECORD_COUNT, len(records))]:
        if len(record) < 20:
            continue
        _, pred, succ = struct.unpack_from("<III", record, 0)
        if pred and succ:
            kind = struct.unpack_from("<H", record, 12)[0]
            unit = struct.unpack_from("<H", record, 14 if old_layout else 18)[0]
            lag = struct.unpack_from("<i", record, 16 if old_layout else 14)[0]
            result.append((pred, succ, kind, unit, lag))
    return result


def _mpp_resource_values(path: str | Path):
    """Read raw percentages: pymppwriter 0.4.3 maps stored zero to 100%."""
    from pymppwriter import blocks
    from pymppwriter.reader import _Klass, _Reader
    from pymppwriter.writer import ASSN_META_SIZE, ASSN_NATIVE, RSC_META_SIZE, RSC_NATIVE
    reader = _Reader(str(path))
    resource_records = _Klass(reader, "   114/TBkndRsc", RSC_META_SIZE, 53, blocks.PROPS_RESOURCE_FIELD_MAP,
                              blocks.PROPS_RESOURCE_RECORD_COUNT)
    assignment_records = _Klass(reader, "   114/TBkndAssn", ASSN_META_SIZE, 53, blocks.PROPS_ASSIGNMENT_FIELD_MAP,
                                blocks.PROPS_ASSN_RECORD_COUNT)
    capacities = {}
    for i in resource_records.live_rows():
        if len(resource_records.recs[i]) <= 100:
            continue
        uid = resource_records.value(i, RSC_NATIVE["UNIQUE_ID"], "<I")
        if uid:
            value = resource_records.value(i, RSC_NATIVE["MAX_UNITS"], "<d")
            if value is not None:
                capacities[uid] = value / 100
    allocations = []
    for i in assignment_records.live_rows():
        task_uid = assignment_records.value(i, ASSN_NATIVE["TASK_UNIQUE_ID"], "<I")
        resource_uid = assignment_records.value(i, ASSN_NATIVE["RESOURCE_UNIQUE_ID"], "<i")
        if task_uid and resource_uid is not None and resource_uid > 0:
            value = assignment_records.value(i, ASSN_NATIVE["UNITS"], "<d")
            allocations.append((task_uid, resource_uid, None if value is None else value / 100))
    return capacities, allocations


def _mpp_task_work(path: str | Path) -> dict[int, tuple[int | None, float | None]]:
    """Read task type and work, which pymppwriter's public reader omits."""
    from pymppwriter import blocks
    from pymppwriter.reader import _Klass, _Reader
    from pymppwriter.writer import NATIVE, TASK_META_SIZE, TASK_META2_SIZE
    reader = _Reader(str(path))
    tasks = _Klass(reader, "   114/TBkndTask", TASK_META_SIZE, TASK_META2_SIZE, blocks.PROPS_TASK_FIELD_MAP,
                   blocks.PROPS_TASK_RECORD_COUNT)
    result = {}
    for i in tasks.live_rows():
        if len(tasks.recs[i]) <= 100:
            continue
        uid = tasks.value(i, NATIVE["UNIQUE_ID"], "<I")
        if uid:
            task_type = tasks.value(i, NATIVE["TYPE"], "<H")
            work = tasks.value(i, NATIVE["WORK"], "<d")
            result[uid] = (task_type, None if work is None else work / 60000)
    return result


def import_mpp(path: str | Path) -> tuple[Plan, list[str]]:
    source = read_project(str(path))
    notes: list[str] = []
    capacities, raw_assignments = _mpp_resource_values(path)
    task_work = _mpp_task_work(path)
    resources = []
    used_names = set()
    for resource in source.resources:
        name = resource.name.strip()
        if name != resource.name:
            notes.append(f"Resource {resource.uid}: trimmed surrounding whitespace from its name")
        if name in used_names:
            original = name
            name = f"{original} (ID {resource.uid})"
            while name in used_names:
                name += "_"
            notes.append(f"Resource {resource.uid}: renamed duplicate {original!r} to {name!r}")
        used_names.add(name)
        resources.append(Resource(resource.uid, name, capacities.get(resource.uid, resource.max_units * 100)))
    resource_ids = {resource.uid for resource in resources}
    task_ids = {task.uid for task in source.tasks}
    numbers = task_numbers(source.tasks)
    if len(raw_assignments) != len(source.assignments):
        raise ValueError("Cannot safely identify MPP resource assignment units")
    assignments: dict[int, list] = {}
    for assignment, (task_uid, resource_uid, units) in zip(source.assignments, raw_assignments):
        if (assignment.task_uid, assignment.resource_uid) != (task_uid, resource_uid):
            raise ValueError("Cannot safely match MPP resource assignments")
        if task_uid not in task_ids or resource_uid not in resource_ids:
            notes.append(f"Omitted assignment to missing task UID {task_uid} or resource {resource_uid}")
            continue
        if units is None:
            units = assignment.units * 100
        if units <= 0:
            notes.append(f"Task {numbers[task_uid]}: omitted resource {resource_uid} assignment with {units:g}% allocation")
            continue
        rows = assignments.setdefault(task_uid, [])
        existing = next((row for row in rows if row.resource_uid == resource_uid), None)
        if existing:
            existing.units += units
            notes.append(f"Task {numbers[task_uid]}: merged duplicate assignments for resource {resource_uid}")
        else:
            rows.append(ResourceAssignment(resource_uid, units))
    links: dict[int, list[int]] = {}
    records = _mpp_relation_records(path)
    if len(records) != len(source.relations):
        raise ValueError("Cannot safely identify the MPP dependency lag units")
    for rel, (pred, succ, kind, unit, raw_lag) in zip(source.relations, records):
        if (pred, succ) != (rel.pred_uid, rel.succ_uid):
            raise ValueError("Cannot safely match the MPP dependencies")
        if kind not in (0, 1, 2, 3) or (raw_lag and unit != 7):
            successor = numbers.get(succ, f"UID {succ}")
            predecessor = numbers.get(pred, f"UID {pred}")
            notes.append(f"Task {successor}: omitted dependency {predecessor} with unsupported MPP link type or lag unit ({unit})")
            continue
        links.setdefault(succ, []).append(format_dependency(Dependency(pred, rel.type, raw_lag / 4800, "d")))
    ids = {task.uid for task in source.tasks}
    summary_ids = {task.parent_uid for task in source.tasks if task.parent_uid}
    pin_reasons: dict[int, str] = {}
    tasks = []
    for task in source.tasks:
        assns = assignments.get(task.uid, [])
        if task.uid in summary_ids and assns:
            notes.append(f"Task {numbers[task.uid]}: summary resource assignments are retained but not scheduled")
        task_type, hours = task_work.get(task.uid, (None, None))
        mode = "duration"
        if task.uid not in summary_ids and task_type == 2:
            if assns and hours is not None and math.isfinite(hours) and hours >= 0 and (hours > 0 or task.duration_days == 0):
                mode = "work"
            else:
                notes.append(f"Task {numbers[task.uid]}: fixed-work data could not be represented; imported as fixed-duration")
        elif task.uid not in summary_ids and task_type == 0:
            # Fixed units keeps the stored duration and derives work from it, which is
            # exactly what the file holds, so nothing is lost on import.
            mode = "units"
        elif task.uid not in summary_ids and task_type not in (0, 1, 2) and assns:
            notes.append(f"Task {numbers[task.uid]}: unknown task type is approximated as fixed-duration")
        predecessors = []
        for pred in links.get(task.uid, []):
            link = parse_dependency(pred)
            if link.uid not in ids:
                notes.append(f"Task {numbers[task.uid]}: omitted dependency on missing task UID {link.uid}")
            elif task.uid in summary_ids and link.kind not in ("FS", "SS"):
                notes.append(f"Task {numbers[task.uid]}: omitted {link.kind} dependency on task "
                             f"{numbers[link.uid]}; a summary task supports only FS and SS links")
            else:
                predecessors.append(pred)
        # What Project recorded decides whether the stored start is history or an instruction.
        # A started or complete, manually scheduled or Must-Start/Finish-On task keeps its date
        # whatever its predecessors say, which is how Project itself treats it. Every other
        # task starts as a link-driven task; its start hint is chosen below from the dates.
        reason = None if task.uid in summary_ids else _pin_reason(task)
        if reason:
            pin_reasons[task.uid] = reason
        tasks.append(Task(uid=task.uid, name=task.name or f"Task {numbers[task.uid]}", parent=task.parent_uid,
                          earliest=task.start.date().isoformat() if reason else "",
                          start_mode="fixed" if reason else "no_earlier",
                          duration=max(0, task.duration_days),
                          mode=mode, work=hours if mode == "work" else 8.0,
                          assignments=assns, predecessors=predecessors, estimated=task.estimated,
                          percent_complete=getattr(task, "percent_complete", 0) or 0))
    # The scheduler never starts a task before the project start, so a file whose recorded
    # start is later than some of its tasks would push them all later. Start the plan at
    # the earliest task instead, and say so.
    project_start = source.start.date()
    first_task = min((task.start.date() for task in source.tasks), default=project_start)
    if first_task < project_start:
        notes.append(f"Project start moved from {project_start} to {first_task}, "
                     "the earliest task start in the file")
        project_start = first_task
    plan = Plan(title=source.title or "Imported project", start=project_start.isoformat(),
                tasks=tasks, resources=resources)
    stored = {task.uid: task.start.date() for task in source.tasks}
    # Choose each remaining task's start hint in one pass, in dependency order, so that a
    # task is compared with what its already-placed predecessors give. Where the links
    # reproduce the stored date there is no hint, so later edits flow through the links.
    # A later stored date becomes a floor. An earlier one is a link the file itself breaks,
    # so the task is fixed at its stored date rather than moved.
    pinned_by_dates: list[int] = []

    def choose_hint(task: Task, free) -> None:
        day = stored[task.uid]
        if task.uid in pin_reasons:
            return
        if free.date() > day:
            task.start_mode, task.earliest = "fixed", day.isoformat()
            pinned_by_dates.append(task.uid)
        elif free.date() < day:
            task.earliest = day.isoformat()

    scheduled = schedule(plan, on_leaf=choose_hint)  # Also rejects broken outlines or cycles.
    if pin_reasons:
        counts = Counter(pin_reasons.values())
        parts = ", ".join(f"{count} {label}" for label, count in counts.most_common())
        notes.append(f"Tasks fixed at their file dates because Project records them as {parts}: {len(pin_reasons)}. "
                     "Predecessors do not move them")
    if pinned_by_dates:
        first = pinned_by_dates[0]
        notes.append(f"Tasks also fixed at their file dates because the file starts them before their "
                     f"predecessors allow: {len(pinned_by_dates)}; the first is Task {numbers[first]} ({stored[first]})")
    conflicts = start_conflicts(plan, scheduled)
    if conflicts:
        first = next(task.uid for task in plan.tasks if task.uid in conflicts)
        notes.append(f"Fixed tasks that start before their predecessors finish, as in the file: {len(conflicts)}. "
                     f"The links are kept; the first is Task {numbers[first]}")
    # By construction every leaf now lands on its stored date. Say so if any does not,
    # for example when a stored date is not a working day, instead of moving silently.
    moved = [task for task in plan.tasks
             if task.uid not in summary_ids and scheduled[task.uid].start.date() != stored[task.uid]]
    if moved:
        first = moved[0]
        notes.append(f"Recalculation moves {_tasks(len(moved))} off the dates in the MPP file; the first is Task "
                     f"{numbers[first.uid]} ({stored[first.uid]} became {scheduled[first.uid].start.date()})")
    return plan, notes


def _tasks(count: int) -> str:
    return f"{count} task" + ("" if count == 1 else "s")


def _pin_reason(task) -> str | None:
    """Why Project holds this task's stored start, or None if its links and hints decide it."""
    if getattr(task, "manual", False):
        return "manually scheduled"
    if getattr(task, "constraint", None) in ("MSO", "MFO"):
        return "Must Start On" if task.constraint == "MSO" else "Must Finish On"
    if (getattr(task, "percent_complete", 0) or 0) > 0:
        return "started"
    return None


def export_mpp(plan: Plan, template: str | Path, destination: str | Path,
               protected_paths: tuple[str | Path, ...] = ()) -> list[str]:
    target, template_path = Path(destination).resolve(), Path(template).resolve()
    protected = {template_path, *(Path(p).resolve() for p in protected_paths)}
    if target in protected:
        raise ValueError("Choose a new MPP file; the template and original imported file cannot be overwritten")
    if target.exists():
        raise ValueError("Choose a new file name; MPP export does not overwrite existing files")
    plan = copy.deepcopy(plan)
    migrate_legacy_resources(plan)
    scheduled = schedule(plan)
    estimates = estimated_tasks(plan)
    progress = completion_percentages(plan, scheduled)
    numbers = task_numbers(plan.tasks)
    by_uid = {task.uid: task for task in plan.tasks}
    resources = [MppResource(uid=resource.uid, name=resource.name, max_units=resource.max_units / 100)
                 for resource in plan.resources]
    assignments: list[MppAssignment] = []
    out_tasks: list[MppTask] = []
    for task in plan.tasks:
        item = scheduled[task.uid]
        level, parent = 1, task.parent
        while parent:
            level += 1
            parent = by_uid[parent].parent
        percent = round(progress[task.uid])
        # A fixed start is a Must Start On constraint, so Project keeps the date whatever the
        # links say. A task with progress needs no constraint: pymppwriter records its start as
        # the actual start, which Project also holds. Other tasks keep the writer's default,
        # a Start No Earlier Than constraint wherever the links do not already place them.
        pin = task.start_mode == "fixed" and not item.summary and percent == 0
        out_tasks.append(MppTask(uid=task.uid, name=task.name, start=item.start, finish=item.finish,
                                 duration_days=item.duration, outline_level=level, parent_uid=task.parent,
                                 estimated=estimates[task.uid],
                                 percent_complete=percent,
                                 task_type=EXPORT_TASK_TYPES[task.mode],
                                 effort_driven=task.mode == "work",
                                 constraint="MSO" if pin else None,
                                 constraint_date=item.start if pin else None))
        if item.summary and task.assignments:
            raise ValueError(f"Cannot export task {numbers[task.uid]}: summary resource assignments are not supported")
        for assignment in task.assignments:
            assignments.append(MppAssignment(task.uid, assignment.resource_uid, units=assignment.units / 100))
    relations = []
    for task in plan.tasks:
        for value in task.predecessors:
            link = parse_dependency(value)
            if link.lag and (link.unit.startswith("e") or link.unit == "%"):
                raise ValueError("MPP export cannot preserve elapsed or percentage lags with pymppwriter; "
                                 "save a .pxplan or change those dependencies first")
            minutes, _ = lag_minutes(link, scheduled[link.uid].duration)
            if abs(minutes * 10 - round(minutes * 10)) > 1e-6:
                raise ValueError("MPP export supports lag precision of one tenth of a minute")
            relations.append(Relation(link.uid, task.uid, type=link.kind, lag_days=minutes / 480))
    # Explicitly write the selected working calendar, including holiday exceptions.
    from pymppwriter import Calendar, CalendarException
    from datetime import date
    calendar = Calendar(exceptions=[CalendarException(date.fromisoformat(day)) for day in plan.holidays])
    project = Project(title=plan.title, start=datetime.fromisoformat(plan.start + "T08:00"),
                      tasks=out_tasks, relations=relations, resources=resources,
                      assignments=assignments, calendar=calendar)
    notes = [f"Task {numbers[task.uid]}: custom collapsed-summary color is not preserved in MPP"
             for task in plan.tasks if task.summary_color]
    conflicts = start_conflicts(plan, scheduled)
    if conflicts:
        notes.append(f"Fixed tasks that start before their predecessors finish: {len(conflicts)}. The links are "
                     "written unchanged and the fixed dates are kept")
    # pymppwriter warns that Project will move a task that starts before its links allow. That
    # does not apply to a fixed task, which is written as a constraint or as progress.
    fixed = {task.uid for task in plan.tasks if task.start_mode == "fixed"}
    target.parent.mkdir(parents=False, exist_ok=True)
    # Publish with a no-overwrite hard link, even if another process creates the
    # destination between the existence check and the end of the write.
    fd, temporary = tempfile.mkstemp(prefix=".onepyproject-", suffix=".mpp", dir=target.parent)
    os.close(fd)
    try:
        with warnings.catch_warnings(record=True) as found:
            warnings.simplefilter("always")
            writer = MppWriter(str(template_path))
            strip_template_resources(writer)
            writer.write(project, temporary)
        os.link(temporary, target)
        messages = []
        for item in found:
            text = str(item.message)
            match = re.match(r"task (\d+) ", text)
            if match and int(match.group(1)) in fixed and "Project will move it" in text:
                continue
            messages.append(text)
        return notes + messages
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
