"""Lossy MPP interchange. The OnePyProject plan is the editable source of truth."""
from __future__ import annotations

from collections import Counter
import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
from .workcalendar import MAX_NAME_LENGTH, MAX_SPAN_DAYS, MAX_TOTAL_DAYS, Period, format_period, parse_period


IMPORT_WARNING = (
    "MPP import is experimental and lossy. The reader can skip unscheduled tasks and "
    "does not reliably read task types or all Project fields. The project calendar's holidays and "
    "working days are imported, but the working week and hours are fixed. This app "
    "recalculates using its own limited scheduler. Costs, custom fields, "
    "baselines, resource and task calendars, rates, timephased work, some lag units and other data may be lost. "
    "Keep the original MPP and verify the imported schedule."
)
EXPORT_TASK_TYPES = {"duration": "fixed_duration", "work": "fixed_work", "units": "fixed_units"}
# Project stores every lag as working tenths of a minute; the unit code only says how it is
# displayed. These units therefore read exactly like days, keeping the unit the file shows.
# Elapsed (4, 6, 8, 10, 12) and percentage (19, 20) units are not working time and stay omitted.
WORKING_LAG_UNITS = {3: ("m", 10), 5: ("h", 600), 7: ("d", 4800), 9: ("w", 24000), 11: ("mo", 96000)}
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


@dataclass
class ProjectCalendar:
    """The part of an MPP project calendar this app can model, and what it left out."""
    name: str = ""
    holidays: list[Period] = field(default_factory=list)
    working_days: list[Period] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# A calendar definition is seven 60-byte day blocks (Sunday first) then a count and the exception
# records (see pymppwriter.blocks.build_calendar_data). An exception record is 92 bytes plus its
# UTF-16 name padded to four bytes: first and last day (days after 1983-12-31) and the number of
# days, then a day block at +12 holding the shifts that are worked on those days (none for a
# holiday), then the recurrence fields. A plain, non-recurring range has +6 zero and one occurrence
# at +72. Recurring exceptions have never been seen in a real file here, so any record that does
# not have that shape is reported and not imported.
CALENDAR_WEEK_SIZE = 7 * 60
CALENDAR_EXCEPTION_SIZE = 92
CALENDAR_DAY_BLOCK = 60
STANDARD_SHIFTS = [(4800, 7200), (7800, 10200)]    # 08:00-12:00 and 13:00-17:00, in tenths of a minute
STANDARD_HOURS = "08:00-12:00 and 13:00-17:00"


def _shifts(data: bytes, base: int) -> list[tuple[int, int]] | None:
    """The worked (start, finish) pairs of a day block at ``base``, in tenths of a minute."""
    count = struct.unpack_from("<H", data, base + 2)[0]
    if count > 5:
        return None
    result = []
    for i in range(count):
        start = struct.unpack_from("<H", data, base + 8 + 2 * i)[0]
        result.append((start, start + struct.unpack_from("<I", data, base + 20 + 4 * i)[0]))
    return result


def _is_standard_week(blob: bytes) -> bool:
    for index in range(7):
        base = index * CALENDAR_DAY_BLOCK
        if struct.unpack_from("<H", blob, base)[0] == 1:
            continue                                  # the default for this weekday
        weekday = (index + 6) % 7                     # block 0 is Sunday
        if _shifts(blob, base) != (STANDARD_SHIFTS if weekday < 5 else []):
            return False
    return True


def _merge_periods(periods: list[Period]) -> list[Period]:
    """Join overlapping periods, so that the lists a person edits never overlap."""
    merged: list[Period] = []
    for period in sorted(periods, key=lambda item: (item.start, item.finish)):
        if merged and period.start <= merged[-1].finish:
            last = merged[-1]
            if period.finish > last.finish:
                merged[-1] = Period(last.start, period.finish, last.name)
        else:
            merged.append(period)
    return merged


def _calendar_name(text: str) -> str:
    """A name this app can store: no commas, control characters or leading dots."""
    text = re.sub(r"\s+", " ", re.sub(r"[,\x00-\x1f\x7f]+", " ", text)).strip().lstrip(".").strip()
    return text[:MAX_NAME_LENGTH].strip()


def _read_exceptions(blob: bytes, result: ProjectCalendar) -> None:
    from pymppwriter import blocks
    epoch = blocks.EPOCH.date()
    position = CALENDAR_WEEK_SIZE
    if len(blob) < position + 4:
        return
    count = struct.unpack_from("<I", blob, position)[0]
    position += 4
    holidays: list[Period] = []
    working: list[Period] = []
    other_hours: list[str] = []
    not_representable: list[str] = []
    for _ in range(count):
        if position + CALENDAR_EXCEPTION_SIZE > len(blob):
            raise ValueError("the calendar exceptions run past the end of the data")
        first, last, days = struct.unpack_from("<HHH", blob, position)
        recurrence = struct.unpack_from("<H", blob, position + 6)[0]
        occurrences = struct.unpack_from("<I", blob, position + 72)[0]
        name_length = struct.unpack_from("<I", blob, position + 88)[0]
        end = position + CALENDAR_EXCEPTION_SIZE + name_length
        if end > len(blob):
            raise ValueError("a calendar exception name runs past the end of the data")
        name = _calendar_name(blob[position + CALENDAR_EXCEPTION_SIZE:end].decode("utf-16-le", "replace"))
        shifts = _shifts(blob, position + 12)
        position = end + (-name_length) % 4
        start, finish = epoch + timedelta(days=first), epoch + timedelta(days=last)
        label = f"{start.isoformat()}" + (f"..{finish.isoformat()}" if finish != start else "")
        label += f" {name}" if name else ""
        if (last < first or days != last - first + 1 or last - first + 1 > MAX_SPAN_DAYS
                or recurrence or occurrences != 1 or shifts is None):
            not_representable.append(label)
        elif not shifts:
            holidays.append(Period(start, finish, name))
        elif shifts == STANDARD_SHIFTS:
            working.append(Period(start, finish, name))
        else:
            other_hours.append(label)
    holidays, working = _merge_periods(holidays), _merge_periods(working)
    holiday_days = {day for period in holidays for day in period.days()}
    clashing = [period for period in working if any(day in holiday_days for day in period.days())]
    if clashing:
        not_representable += [format_period(period) for period in clashing]
        working = [period for period in working if period not in clashing]
    if sum(period.length for period in holidays) > MAX_TOTAL_DAYS or sum(period.length for period in working) > MAX_TOTAL_DAYS:
        raise ValueError("the calendar lists too many days")
    result.holidays, result.working_days = holidays, working
    if other_hours:
        result.notes.append(f"{_count(len(other_hours), 'exception')} with working hours other than {STANDARD_HOURS} "
                            f"{'is' if len(other_hours) == 1 else 'are'} not imported, so those days are worked as normal "
                            f"days; the first is {other_hours[0]}")
    if not_representable:
        result.notes.append(f"{_count(len(not_representable), 'exception')} that cannot be represented (recurring, "
                            f"overlapping or very long) {'is' if len(not_representable) == 1 else 'are'} not "
                            f"imported; the first is {not_representable[0]}")


def _mpp_calendar(path: str | Path) -> ProjectCalendar:
    """Read the project calendar's holidays and working days, and report what this app does not model.

    The week is fixed in this app, so a different working week, task calendars and resource
    calendars with their own working time are reported rather than applied.
    """
    from pymppwriter import blocks
    from pymppwriter.reader import _Klass, _Reader
    from pymppwriter.writer import CAL_DATA_VAR, CAL_NAME_VAR, NATIVE, TASK_META2_SIZE, TASK_META_SIZE
    reader = _Reader(str(path))
    base = "   114/TBkndCal"
    try:
        table = blocks.parse_var_meta(reader._get(base + "/VarMeta"))[1]
        data = reader._get(base + "/Var2Data")
    except KeyError:
        return ProjectCalendar(notes=["the file has no calendar data, so no holidays or working days were imported"])
    default = reader.props.get(blocks.PROPS_DEFAULT_CALENDAR_NAME)
    wanted = blocks.decode_unicode(default) if default else "Standard"
    names = {uid: blocks.decode_unicode(blocks.read_var(data, entries[CAL_NAME_VAR]))
             for uid, entries in table.items() if CAL_NAME_VAR in entries}
    uid = next((candidate for candidate, name in sorted(names.items()) if name == wanted), None)
    if uid is None:
        return ProjectCalendar(name=wanted, notes=[f"the project calendar {wanted!r} was not found, so no holidays or "
                                                   "working days were imported"])
    result = ProjectCalendar(name=wanted)
    if CAL_DATA_VAR in table[uid]:
        blob = blocks.read_var(data, table[uid][CAL_DATA_VAR])
        if len(blob) >= CALENDAR_WEEK_SIZE:
            if not _is_standard_week(blob):
                result.notes.append(f"the working week is not Monday-Friday {STANDARD_HOURS}; this app uses that week")
            _read_exceptions(blob, result)
    # Calendars that other items use are not applied: say so when the file has any.
    unnamed = [other for other, entries in table.items() if other != uid and CAL_DATA_VAR in entries
               and CAL_NAME_VAR not in entries]
    if unnamed:
        result.notes.append(f"{_count(len(unnamed), 'resource calendar')} with its own working time "
                            f"{'is' if len(unnamed) == 1 else 'are'} not applied; everyone uses the project calendar")
    tasks = _Klass(reader, "   114/TBkndTask", TASK_META_SIZE, TASK_META2_SIZE, blocks.PROPS_TASK_FIELD_MAP,
                   blocks.PROPS_TASK_RECORD_COUNT)
    own = sum(1 for i in tasks.live_rows() if len(tasks.recs[i]) > 100
              and (tasks.value(i, NATIVE["CALENDAR_UNIQUE_ID"], "<i") or -1) not in (-1, uid))
    if own:
        result.notes.append(f"{_count(own, 'task')} use a task calendar, which is not applied; "
                            "they are scheduled on the project calendar")
    return result


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def _link_text(value) -> str:
    """Describe a stored dependency without its task ID, e.g. FS or SS+2d."""
    return re.sub(r"^\d+", "", str(value)) or "FS"


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
        if kind not in (0, 1, 2, 3) or (raw_lag and unit not in WORKING_LAG_UNITS):
            successor = numbers.get(succ, f"UID {succ}")
            predecessor = numbers.get(pred, f"UID {pred}")
            notes.append(f"Task {successor}: omitted dependency {predecessor} with unsupported MPP link type or lag unit ({unit})")
            continue
        # A zero lag needs no unit; use days, as the export does.
        unit_name, tenths = WORKING_LAG_UNITS.get(unit, ("d", 4800))
        link = format_dependency(Dependency(pred, rel.type, raw_lag / tenths, unit_name))
        # A task can have one link to each predecessor. A file can still hold several records for the
        # same pair, so identical ones merge and a different one is reported and left out.
        kept = links.setdefault(succ, [])
        earlier = next((item for item in kept if parse_dependency(item).uid == pred), None)
        if earlier is None:
            kept.append(link)
        elif str(earlier) == str(link):
            notes.append(f"Task {numbers.get(succ, f'UID {succ}')}: merged duplicate dependency on "
                         f"{numbers.get(pred, f'UID {pred}')}")
        else:
            notes.append(f"Task {numbers.get(succ, f'UID {succ}')}: omitted a second dependency on "
                         f"{numbers.get(pred, f'UID {pred}')} ({_link_text(link)}); kept {_link_text(earlier)}")
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
    # Holidays and working days must be known before any date is chosen, because the start
    # hints below compare each stored date with what the links give on this calendar.
    try:
        calendar = _mpp_calendar(path)
    except (KeyError, IndexError, ValueError, struct.error) as exc:
        calendar = ProjectCalendar(notes=[f"the project calendar could not be read ({exc}), so no holidays or "
                                          "working days were imported"])
    imported = [_count(len(calendar.holidays), "holiday period"), _count(len(calendar.working_days), "working day")]
    if calendar.holidays or calendar.working_days:
        notes.append("Imported " + " and ".join(text for text, items in
                                                 zip(imported, (calendar.holidays, calendar.working_days)) if items)
                     + f" from the project calendar {calendar.name!r}")
    notes.extend(f"Project calendar: {text}" for text in calendar.notes)
    plan = Plan(title=source.title or "Imported project", start=project_start.isoformat(),
                tasks=tasks, resources=resources, holidays=[format_period(p) for p in calendar.holidays],
                working_days=[format_period(p) for p in calendar.working_days])
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
    # Explicitly write the selected working calendar, including named holiday periods. Overlapping
    # entries (older plans may repeat a date) are joined so that Project gets one exception per day.
    from pymppwriter import Calendar, CalendarException
    holidays = _merge_periods([parse_period(entry) for entry in plan.holidays])
    calendar = Calendar(exceptions=[CalendarException(period.start, period.finish, period.name)
                                    for period in holidays])
    project = Project(title=plan.title, start=datetime.fromisoformat(plan.start + "T08:00"),
                      tasks=out_tasks, relations=relations, resources=resources,
                      assignments=assignments, calendar=calendar)
    notes = [f"Task {numbers[task.uid]}: custom collapsed-summary color is not preserved in MPP"
             for task in plan.tasks if task.summary_color]
    # pymppwriter writes non-working exceptions only. A working day on a weekday changes nothing, but
    # one on a weekend would be non-working in Project, so say so.
    weekend_work = sorted(day for entry in plan.working_days for day in parse_period(entry).days()
                          if day.weekday() >= 5)
    if weekend_work:
        notes.append(f"Working days on a weekend are not written to the MPP, so Project schedules them as "
                     f"non-working: {len(weekend_work)}; the first is {weekend_work[0].isoformat()}")
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
