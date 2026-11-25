"""Build read-only main-window rows for an AI proposal."""
from __future__ import annotations

import copy
from dataclasses import dataclass

from ..dependencies import remap_predecessors, task_numbers
from ..model import (MODE_LABELS, START_MODE_LABELS, Plan, Scheduled, completion_percentages, estimated_tasks,
                     schedule)
from ..resources import format_task_resources
from .diff import PlanDiff, compare_plans


ADDED = "#e2f4e4"
REMOVED = "#fbe3e3"
CHANGED = "#fff8d9"
CHANGED_CELL = "#ffe9a3"
ROW_COLORS = {"added": ADDED, "removed": REMOVED, "changed": CHANGED}


@dataclass(frozen=True)
class PreviewRow:
    uid: int
    parent: int
    status: str
    values: tuple[str, ...]
    previous: tuple[str, ...] | None
    changed_columns: frozenset[int]
    before: Scheduled | None
    after: Scheduled | None
    details: str
    before_progress: float | None
    after_progress: float | None


@dataclass(frozen=True)
class PreviewData:
    before: Plan
    after: Plan
    rows: tuple[PreviewRow, ...]
    diff: PlanDiff
    before_progress: dict[int, float]
    after_progress: dict[int, float]


def build_preview(before: Plan, after: Plan, format_date) -> PreviewData:
    """Compare detached plans by stable IDs, preserving removed rows and hierarchy."""
    old_plan, new_plan = copy.deepcopy(before), copy.deepcopy(after)
    old_schedule, new_schedule = schedule(old_plan), schedule(new_plan)
    old_numbers, new_numbers = task_numbers(old_plan.tasks), task_numbers(new_plan.tasks)
    old_estimates, new_estimates = estimated_tasks(old_plan), estimated_tasks(new_plan)
    old_progress = completion_percentages(old_plan, old_schedule)
    new_progress = completion_percentages(new_plan, new_schedule)
    old_tasks, new_tasks = {task.uid: task for task in old_plan.tasks}, {task.uid: task for task in new_plan.tasks}
    diff = compare_plans(old_plan, new_plan)

    def values(plan, task, calculated, numbers, estimates, progress):
        timed = 0 < calculated.duration < 1
        return (str(numbers[task.uid]), task.name, f"{calculated.duration:g} d{'?' if estimates[task.uid] else ''}",
                format_date(calculated.start, include_time=timed), format_date(calculated.finish, include_time=timed),
                ", ".join(map(str, remap_predecessors(task.predecessors, numbers))),
                format_task_resources(plan, task), f"{calculated.work:g} h", MODE_LABELS[task.mode],
                f"{progress[task.uid]:.1f}%")

    children = {0: []}
    for task in new_plan.tasks:
        children.setdefault(task.parent, []).append(task.uid)
        children.setdefault(task.uid, [])
    old_siblings = {}
    for task in old_plan.tasks:
        old_siblings.setdefault(task.parent, []).append(task.uid)
    for task in old_plan.tasks:
        if task.uid in new_tasks:
            continue
        siblings = children.setdefault(task.parent, [])
        original = old_siblings[task.parent]
        later = original[original.index(task.uid) + 1:]
        following = next((uid for uid in later if uid in siblings), None)
        siblings.insert(siblings.index(following) if following is not None else len(siblings), task.uid)
        children.setdefault(task.uid, [])

    ordered = []
    stack = list(reversed(children[0]))
    while stack:
        uid = stack.pop()
        ordered.append(uid)
        stack.extend(reversed(children.get(uid, [])))
    if len(ordered) != len(set(old_tasks) | set(new_tasks)) or len(set(ordered)) != len(ordered):
        raise ValueError("Cannot safely display the proposed task hierarchy")

    rows = []
    for uid in ordered:
        old, new = old_tasks.get(uid), new_tasks.get(uid)
        old_item, new_item = old_schedule.get(uid), new_schedule.get(uid)
        old_values = values(old_plan, old, old_item, old_numbers, old_estimates, old_progress) if old else None
        new_values = values(new_plan, new, new_item, new_numbers, new_estimates, new_progress) if new else None
        changed = set()
        details = []
        if old and new:
            changed.update(i for i in range(10) if old_values[i] != new_values[i])
            for column, fields in ((1, ("name",)), (2, ("duration", "estimated")), (5, ("predecessors",)),
                                   (6, ("assignments", "resource", "units")), (7, ("work",)), (8, ("mode",)),
                                   (9, ("percent_complete",))):
                if any(getattr(old, field) != getattr(new, field) for field in fields):
                    changed.add(column)
            if old.parent != new.parent:
                changed.add(1)
                details.append(f"Parent: {old.parent or 'top level'} → {new.parent or 'top level'}")
            if old.earliest != new.earliest:
                changed.add(3)
                details.append(f"Earliest start: {old.earliest or 'none'} → {new.earliest or 'none'}")
            if old.start_mode != new.start_mode:
                changed.add(3)
                details.append(f"Start rule: {START_MODE_LABELS[old.start_mode]} → {START_MODE_LABELS[new.start_mode]}")
            for column, field in ((2, "duration"), (3, "start"), (4, "finish"), (7, "work")):
                if getattr(old_item, field) != getattr(new_item, field):
                    changed.add(column)
            if old.mode != new.mode:
                changed.add(8)
            if old.estimated != new.estimated:
                changed.add(2)
            status = "changed" if changed or old != new or old_item != new_item else "unchanged"
        else:
            status = "added" if new else "removed"
        rows.append(PreviewRow(uid, new.parent if new else old.parent, status,
                               new_values if new else old_values, old_values,
                               frozenset(changed), old_item, new_item, "\n".join(details),
                               old_progress.get(uid), new_progress.get(uid)))
    return PreviewData(old_plan, new_plan, tuple(rows), diff, old_progress, new_progress)
