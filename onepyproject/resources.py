"""Resource migration, editable assignments, and overallocation detection."""
from __future__ import annotations

import copy
import csv
from dataclasses import dataclass
from datetime import datetime
import io
import math
import re

from .model import Plan, Resource, ResourceAssignment, Task, schedule, work_between
from .workcalendar import work_calendar


def migrate_legacy_resources(plan: Plan) -> None:
    """Upgrade old one-resource tasks in place without merging new assignments."""
    by_name = {resource.name.strip(): resource for resource in plan.resources}
    next_uid = max((resource.uid for resource in plan.resources), default=0) + 1
    for task in plan.tasks:
        name = task.resource.strip()
        if not name:
            continue
        if task.assignments:
            raise ValueError(f"Task {task.uid}: cannot mix old and new resource assignments")
        resource = by_name.get(name)
        if resource is None:
            resource = Resource(next_uid, name)
            next_uid += 1
            by_name[name] = resource
            plan.resources.append(resource)
        task.assignments = [ResourceAssignment(resource.uid, task.units)]
        task.resource = ""
        task.units = 100.0


def format_task_resources(plan: Plan, task: Task) -> str:
    if task.resource.strip() and not task.assignments:
        pairs = [(task.resource.strip(), task.units)]
    else:
        by_uid = {resource.uid: resource for resource in plan.resources}
        pairs = [(by_uid[a.resource_uid].name, a.units) for a in task.assignments]
    parts = [name if units == 100 else f"{name}[{str(float(units)).removesuffix('.0')}%]" for name, units in pairs]
    encoded = []
    for part in parts:
        stream = io.StringIO(newline="")
        csv.writer(stream, lineterminator="").writerow([part])
        encoded.append(stream.getvalue())
    return ", ".join(encoded)


_ALLOCATION = re.compile(r"^(.*?)\s*\[([+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:e[+-]?\d+)?)%\]\s*$", re.IGNORECASE)


def set_task_resources(plan: Plan, task: Task, text: str) -> None:
    """Replace assignments from names and optional percentages; add new names."""
    migrate_legacy_resources(plan)
    by_name = {resource.name.strip(): resource for resource in plan.resources}
    def find_resource(name):
        if name in by_name:
            return by_name[name]
        matches = [resource for existing, resource in by_name.items() if existing.casefold() == name.casefold()]
        if len(matches) > 1:
            raise ValueError(f"Ambiguous resource {name!r}; use its exact name or the Assign dialog")
        return matches[0] if matches else None
    if not text.strip():
        task.assignments = []
        return
    try:
        parts = next(csv.reader([text], skipinitialspace=True, strict=True))
    except csv.Error as exc:
        raise ValueError(f"Invalid resource list: {exc}") from exc
    allocations = []
    for part in parts:
        entry = part.strip()
        if not entry:
            raise ValueError("Resource names cannot be empty")
        match = _ALLOCATION.fullmatch(entry)
        exact = find_resource(entry)
        base = find_resource(match[1].strip()) if match else None
        if exact and base and exact.uid != base.uid:
            raise ValueError(f"Ambiguous resource {entry!r}; use the Assign dialog")
        if exact:
            name, units = exact.name, 100.0
        elif match:
            name, units = match[1].strip(), float(match[2])
        else:
            name, units = entry, 100.0
        if not name or not math.isfinite(units) or units <= 0:
            raise ValueError("Resource names must be non-empty and allocation must be positive")
        resource = find_resource(name)
        if resource is None:
            resource = Resource(max((r.uid for r in plan.resources), default=0) + 1, name)
            by_name[name] = resource
            plan.resources.append(resource)
        if any(assignment.resource_uid == resource.uid for assignment in allocations):
            raise ValueError(f"Resource {resource.name} is assigned more than once")
        allocations.append(ResourceAssignment(resource.uid, units))
    task.assignments = allocations


@dataclass(frozen=True)
class Overallocation:
    start: datetime
    finish: datetime
    units: float
    capacity: float


def overallocation_warnings(plan: Plan) -> dict[int, list[Overallocation]]:
    """Find overlapping work assignments above capacity; never reschedule them."""
    snapshot = copy.deepcopy(plan)
    migrate_legacy_resources(snapshot)
    calculated = schedule(snapshot)
    calendar = work_calendar(snapshot)
    events: dict[int, dict[datetime, float]] = {}
    for task in snapshot.tasks:
        item = calculated[task.uid]
        if item.summary or item.start == item.finish:
            continue
        for assignment in task.assignments:
            resource_events = events.setdefault(assignment.resource_uid, {})
            resource_events[item.start] = resource_events.get(item.start, 0) + assignment.units
            resource_events[item.finish] = resource_events.get(item.finish, 0) - assignment.units
    result: dict[int, list[Overallocation]] = {}
    for resource in snapshot.resources:
        points = sorted(events.get(resource.uid, {}).items())
        total = 0.0
        for i, (start, change) in enumerate(points[:-1]):
            total += change
            finish = points[i + 1][0]
            if total > resource.max_units + 1e-9 and work_between(start, finish, calendar) > 0:
                result.setdefault(resource.uid, []).append(Overallocation(start, finish, total, resource.max_units))
    return result
