"""Deterministic comparison of working and proposed project snapshots."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime

from ..dependencies import remap_predecessors, task_numbers
from ..model import Plan, Scheduled, completion_percentages, schedule
from ..resources import format_task_resources, migrate_legacy_resources, overallocation_warnings


@dataclass(frozen=True)
class Change:
    category: str
    item: str
    field: str
    before: str
    after: str


@dataclass(frozen=True)
class TimelineRow:
    uid: int
    label: str
    status: str
    before: Scheduled | None
    after: Scheduled | None


@dataclass(frozen=True)
class PlanDiff:
    changes: tuple[Change, ...]
    timeline: tuple[TimelineRow, ...]
    start: datetime | None
    finish: datetime | None


def _value(value) -> str:
    if value is None or value == "" or value == []:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="auto")
    if isinstance(value, float):
        return str(value)
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return str(value)


def compare_plans(before: Plan, after: Plan) -> PlanDiff:
    """Compare internal IDs, not displayed row numbers, including schedule ripple."""
    old_schedule, new_schedule = schedule(before), schedule(after)
    old_progress = completion_percentages(before, old_schedule)
    new_progress = completion_percentages(after, new_schedule)
    old_numbers, new_numbers = task_numbers(before.tasks), task_numbers(after.tasks)
    old_tasks, new_tasks = {t.uid: t for t in before.tasks}, {t.uid: t for t in after.tasks}
    old_resources, new_resources = {r.uid: r for r in before.resources}, {r.uid: r for r in after.resources}
    changes = []

    def add(category, item, field, old, new):
        changes.append(Change(category, item, field, _value(old), _value(new)))

    for field, label in (("title", "Project name"), ("start", "Project start"), ("holidays", "Holidays")):
        old, new = getattr(before, field), getattr(after, field)
        if old != new:
            add("Direct", "Project", label, old, new)
    for uid in dict.fromkeys([r.uid for r in before.resources] + [r.uid for r in after.resources]):
        old, new = old_resources.get(uid), new_resources.get(uid)
        label = f"Resource: {(new or old).name}"
        if old is None:
            add("Direct", label, "Added", None, f"{new.name} ({new.max_units:g}% capacity)")
        elif new is None:
            add("Direct", label, "Removed", f"{old.name} ({old.max_units:g}% capacity)", None)
        else:
            for field, description in (("name", "Name"), ("max_units", "Capacity %")):
                if getattr(old, field) != getattr(new, field):
                    add("Direct", label, description, getattr(old, field), getattr(new, field))

    def task_label(uid):
        old, new = old_tasks.get(uid), new_tasks.get(uid)
        number = str(new_numbers[uid]) if new else str(old_numbers[uid])
        if old and new and old_numbers[uid] != new_numbers[uid]:
            number = f"{old_numbers[uid]} → {new_numbers[uid]}"
        return f"Task {number}: {(new or old).name}"

    def parent_value(plan, numbers, task):
        if not task.parent:
            return "Top level"
        parent = next(item for item in plan.tasks if item.uid == task.parent)
        return f"{numbers[task.parent]}: {parent.name}"

    def field_value(plan, numbers, task, field):
        if field == "parent":
            return parent_value(plan, numbers, task)
        if field == "predecessors":
            return ", ".join(map(str, remap_predecessors(task.predecessors, numbers))) or "—"
        if field == "assignments":
            return format_task_resources(plan, task) or "—"
        return getattr(task, field)

    fields = (("name", "Name"), ("parent", "Parent"), ("duration", "Duration days"),
              ("earliest", "Earliest start"), ("mode", "Mode"), ("work", "Work hours"),
              ("estimated", "Estimated"), ("percent_complete", "Complete %"),
              ("summary_color", "Collapsed summary color"),
              ("predecessors", "Predecessors"), ("assignments", "Assignments"),
              ("resource", "Legacy resource"), ("units", "Legacy allocation %"))
    order_changed = ([task.uid for task in before.tasks if task.uid in new_tasks]
                     != [task.uid for task in after.tasks if task.uid in old_tasks])
    for uid in dict.fromkeys([task.uid for task in after.tasks] + [task.uid for task in before.tasks]):
        old, new = old_tasks.get(uid), new_tasks.get(uid)
        label = task_label(uid)
        if old is None or new is None:
            task, plan, numbers = (new, after, new_numbers) if old is None else (old, before, old_numbers)
            add("Direct", label, "Added" if old is None else "Removed", None if old is None else task.name,
                task.name if old is None else None)
            for field, description in fields:
                if field in ("name", "resource", "units"):
                    continue
                if field == "percent_complete" and (new_schedule[uid] if new is not None else old_schedule[uid]).summary:
                    continue
                value = field_value(plan, numbers, task, field)
                add("Direct", label, description, None if old is None else value, value if old is None else None)
            item = new_schedule[uid] if old is None else old_schedule[uid]
            for field, description in (("start", "Scheduled start"), ("finish", "Scheduled finish"),
                                       ("duration", "Calculated duration days"), ("work", "Calculated work hours"),
                                       ("progress", "Calculated completion %")):
                value = (old_progress[uid] if old is not None else new_progress[uid]) if field == "progress" else getattr(item, field)
                add("Calculated", label, description, None if old is None else value, value if old is None else None)
            continue
        for field, description in fields:
            if field == "percent_complete" and (old.parent or new.parent):
                continue
            old_value, new_value = getattr(old, field), getattr(new, field)
            if old_value != new_value:
                add("Direct", label, description, field_value(before, old_numbers, old, field),
                    field_value(after, new_numbers, new, field))
        if order_changed and old_numbers[uid] != new_numbers[uid]:
            add("Direct", label, "Position", old_numbers[uid], new_numbers[uid])
        if old_numbers[uid] != new_numbers[uid]:
            add("Calculated", label, "Displayed ID", old_numbers[uid], new_numbers[uid])
        for field, description in (("start", "Scheduled start"), ("finish", "Scheduled finish"),
                                   ("duration", "Calculated duration days"), ("work", "Calculated work hours"),
                                   ("summary", "Summary task"), ("progress", "Calculated completion %")):
            if field == "progress" and not (old_schedule[uid].summary or new_schedule[uid].summary):
                continue
            old_value = old_progress[uid] if field == "progress" else getattr(old_schedule[uid], field)
            new_value = new_progress[uid] if field == "progress" else getattr(new_schedule[uid], field)
            if old_value != new_value:
                add("Calculated", label, description, old_value, new_value)

    def warnings_by_identity(plan):
        normalized = copy.deepcopy(plan)
        migrate_legacy_resources(normalized)
        real_ids = {resource.uid for resource in plan.resources}
        resources = {resource.uid: resource for resource in normalized.resources}
        return {("resource", uid) if uid in real_ids else ("legacy", resources[uid].name):
                (resources[uid].name, values) for uid, values in overallocation_warnings(normalized).items()}

    old_warnings, new_warnings = warnings_by_identity(before), warnings_by_identity(after)
    for identity in dict.fromkeys(list(old_warnings) + list(new_warnings)):
        old_name, old = old_warnings.get(identity, (None, []))
        new_name, new = new_warnings.get(identity, (None, []))
        if old != new:
            label = f"Resource: {new_name or old_name}"
            def describe(values):
                return "; ".join(f"{v.start.isoformat(sep=' ')}–{v.finish.isoformat(sep=' ')}: {v.units}% / {v.capacity}%"
                                 for v in values) or "None"
            add("Calculated", label, "Overallocation", describe(old), describe(new))

    changed_ids = {uid for uid in set(old_tasks) | set(new_tasks)
                   if old_tasks.get(uid) != new_tasks.get(uid) or old_schedule.get(uid) != new_schedule.get(uid)}
    rows = []
    for uid in dict.fromkeys([task.uid for task in after.tasks] + [task.uid for task in before.tasks]):
        status = "Added" if uid not in old_tasks else "Removed" if uid not in new_tasks else "Changed" if uid in changed_ids else "Unchanged"
        rows.append(TimelineRow(uid, task_label(uid), status, old_schedule.get(uid), new_schedule.get(uid)))
    dates = [value for row in rows for item in (row.before, row.after) if item is not None for value in (item.start, item.finish)]
    return PlanDiff(tuple(changes), tuple(rows), min(dates, default=None), max(dates, default=None))
