"""Project model and intentionally limited, deterministic scheduling rules."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import copy
from datetime import date, datetime, time, timedelta
import json
import math
import os
from pathlib import Path
import re
import tempfile

from .dependencies import lag_minutes, parse_dependency, task_numbers


# "duration": duration is the input and work is derived. "work": work is the input and
# duration is derived. "units": resource units are held, so either duration or work may
# be edited and the other recalculated. The scheduler itself always works from the
# stored duration for "duration" and "units"; the difference is how edits propagate.
MODES = ("duration", "work", "units")
MODE_LABELS = {"duration": "Duration", "work": "Work", "units": "Units"}
MODE_NAMES = {"duration": "Fixed duration", "work": "Fixed work", "units": "Fixed units"}


@dataclass
class Resource:
    uid: int
    name: str
    max_units: float = 100.0


@dataclass
class ResourceAssignment:
    resource_uid: int
    units: float = 100.0


@dataclass
class Task:
    uid: int
    name: str = "New task"
    parent: int = 0
    earliest: str = ""
    duration: float = 1.0
    mode: str = "duration"
    work: float = 8.0
    resource: str = ""
    units: float = 100.0
    # Old plans store plain IDs; non-default links use expressions such as 5SS+3d.
    predecessors: list[int | str] = field(default_factory=list)
    estimated: bool = False
    assignments: list[ResourceAssignment] = field(default_factory=list)
    percent_complete: float = 0.0
    summary_color: str = ""


@dataclass
class Plan:
    title: str = "Untitled project"
    start: str = field(default_factory=lambda: date.today().isoformat())
    holidays: list[str] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    resources: list[Resource] = field(default_factory=list)


@dataclass(frozen=True)
class Scheduled:
    start: datetime
    finish: datetime
    duration: float
    work: float
    summary: bool = False


def total_units(task: Task) -> float:
    """Total allocation as a percentage, from assignments or the legacy single resource."""
    if task.resource.strip():
        return task.units
    return sum(assignment.units for assignment in task.assignments)


def estimated_tasks(plan: Plan) -> dict[int, bool]:
    """An estimated descendant also marks its summary duration as estimated."""
    result = {task.uid: task.estimated for task in plan.tasks}
    for task in reversed(plan.tasks):
        if task.parent and result[task.uid]:
            result[task.parent] = True
    return result


def save_plan(plan: Plan, path: str | Path) -> None:
    # Never use the interchange writer to save the editable working copy.
    from .resources import migrate_legacy_resources
    snapshot = copy.deepcopy(plan)
    migrate_legacy_resources(snapshot)
    schedule(snapshot)
    target = Path(path)
    # Keep the established file identifier so existing .pxplan files remain compatible.
    payload = json.dumps({"format": "projectx-plan", "version": 3,
                          "plan": asdict(snapshot)}, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".onepyproject-", suffix=".pxplan", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_plan(path: str | Path) -> Plan:
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    if obj.get("format") != "projectx-plan" or obj.get("version") not in (1, 2, 3):
        raise ValueError("Not a supported OnePyProject plan")
    data = obj["plan"]
    tasks = []
    for item in data["tasks"]:
        values = dict(item)
        values["assignments"] = [ResourceAssignment(**assignment) for assignment in values.get("assignments", [])]
        tasks.append(Task(**values))
    plan = Plan(title=data["title"], start=data["start"], holidays=data.get("holidays", []),
                tasks=tasks, resources=[Resource(**item) for item in data.get("resources", [])])
    from .resources import migrate_legacy_resources
    migrate_legacy_resources(plan)
    schedule(plan)
    return plan


def _is_workday(day: date, holidays: set[date]) -> bool:
    return day.weekday() < 5 and day not in holidays


def next_start(point: datetime, holidays: set[date]) -> datetime:
    """First working instant at or after point, excluding period end boundaries."""
    day = point.date()
    while True:
        if _is_workday(day, holidays):
            for lo, hi in ((time(8), time(12)), (time(13), time(17))):
                start, end = datetime.combine(day, lo), datetime.combine(day, hi)
                candidate = max(point, start)
                if candidate < end:
                    return candidate
        day += timedelta(days=1)


def add_work(start: datetime, minutes: float, holidays: set[date]) -> datetime:
    if minutes <= 0:
        return start
    point = next_start(start, holidays)
    remaining = round(minutes, 6)
    while remaining > 0.000001:
        end_time = time(12) if point.time() < time(12) else time(17)
        end = datetime.combine(point.date(), end_time)
        available = (end - point).total_seconds() / 60
        if remaining <= available:
            return point + timedelta(minutes=remaining)
        remaining -= available
        point = next_start(end, holidays)
    return point


def subtract_work(end: datetime, minutes: float, holidays: set[date]) -> datetime:
    """Move backwards through the working calendar, preserving boundary times."""
    if minutes <= 0:
        return end
    remaining, day = round(minutes, 6), end.date()
    while remaining > 0.000001:
        if _is_workday(day, holidays):
            for lo, hi in ((time(13), time(17)), (time(8), time(12))):
                start = datetime.combine(day, lo)
                stop = min(end, datetime.combine(day, hi))
                available = max(0, (stop - start).total_seconds() / 60)
                if remaining <= available:
                    return stop - timedelta(minutes=remaining)
                remaining -= available
        day -= timedelta(days=1)
    return end


def milestone_at_or_after(point: datetime, holidays: set[date]) -> datetime:
    if _is_workday(point.date(), holidays) and (
        time(8) <= point.time() <= time(12) or time(13) <= point.time() <= time(17)
    ):
        return point
    return next_start(point, holidays)


def offset_time(anchor: datetime, minutes: float, elapsed: bool, holidays: set[date]) -> datetime:
    if elapsed:
        return anchor + timedelta(minutes=minutes)
    if minutes > 0:
        return add_work(anchor, minutes, holidays)
    if minutes < 0:
        return subtract_work(anchor, -minutes, holidays)
    return anchor


def work_between(start: datetime, finish: datetime, holidays: set[date]) -> float:
    result = 0.0
    day = start.date()
    while day <= finish.date():
        if _is_workday(day, holidays):
            for lo, hi in ((time(8), time(12)), (time(13), time(17))):
                begin = max(start, datetime.combine(day, lo))
                end = min(finish, datetime.combine(day, hi))
                result += max(0, (end - begin).total_seconds() / 60)
        day += timedelta(days=1)
    return result


def schedule(plan: Plan) -> dict[int, Scheduled]:
    """Schedule leaf tasks and roll up summaries using the project calendar.

    Dates are local wall-clock times. A summary task may be the predecessor of any
    link type, using its rolled-up dates. A link *to* a summary task (FS or SS only)
    is a start floor on every leaf beneath it, so the summary's own start follows
    from its children. Finish-based links to a summary and resource leveling are
    intentionally unsupported rather than approximated silently.
    """
    project_start = datetime.combine(date.fromisoformat(plan.start), time(8))
    holidays = {date.fromisoformat(value) for value in plan.holidays}
    tasks = {t.uid: t for t in plan.tasks}
    if len(tasks) != len(plan.tasks) or any(uid <= 0 for uid in tasks):
        raise ValueError("Task IDs must be unique positive numbers")
    numbers = task_numbers(plan.tasks)
    resources = {resource.uid: resource for resource in plan.resources}
    if len(resources) != len(plan.resources) or any(uid <= 0 for uid in resources):
        raise ValueError("Resource IDs must be unique positive numbers")
    names = set()
    for resource in plan.resources:
        key = resource.name.strip()
        if not key or "\n" in resource.name or "\r" in resource.name:
            raise ValueError("Resources must have non-empty, single-line names")
        if key in names:
            raise ValueError(f"Duplicate resource name: {resource.name}")
        names.add(key)
        if not math.isfinite(resource.max_units) or resource.max_units < 0:
            raise ValueError(f"Resource {resource.name}: maximum units must be non-negative")
    children: dict[int, list[int]] = {}
    # A preorder list ensures the outline is unambiguous and writer-compatible.
    stack: list[int] = []
    for task in plan.tasks:
        number = numbers[task.uid]
        if not task.name.strip():
            raise ValueError(f"Task {number} needs a name")
        if task.parent:
            while stack and stack[-1] != task.parent:
                stack.pop()
            if not stack:
                raise ValueError(f"Task {number} has an invalid outline")
        else:
            stack.clear()
        children.setdefault(task.parent, []).append(task.uid)
        stack.append(task.uid)
        if task.mode not in MODES:
            raise ValueError(f"Task {number}: invalid scheduling mode")
        if not all(math.isfinite(x) for x in (task.duration, task.work, task.units)):
            raise ValueError(f"Task {number}: numbers must be finite")
        if not math.isfinite(task.percent_complete) or not 0 <= task.percent_complete <= 100:
            raise ValueError(f"Task {number}: percent complete must be between 0 and 100")
        if task.summary_color and not re.fullmatch(r"#[0-9A-Fa-f]{6}", task.summary_color):
            raise ValueError(f"Task {number}: summary color must be a six-digit hex color")
        if (not math.isfinite(task.percent_complete) or
                not 0 <= task.percent_complete <= 100):
            raise ValueError(f"Task {number}: percent complete must be between 0 and 100")
        if task.duration < 0 or task.work < 0 or task.units <= 0:
            raise ValueError(f"Task {number}: duration and work must be non-negative and allocation positive")
        if task.assignments and task.resource.strip():
            raise ValueError(f"Task {number}: cannot mix legacy and current resource assignments")
        assigned = set()
        for assignment in task.assignments:
            if assignment.resource_uid not in resources:
                raise ValueError(f"Task {number}: resource {assignment.resource_uid} does not exist")
            if assignment.resource_uid in assigned:
                raise ValueError(f"Task {number}: resource assignments must be unique")
            assigned.add(assignment.resource_uid)
            if not math.isfinite(assignment.units) or assignment.units <= 0:
                raise ValueError(f"Task {number}: resource allocation must be positive")
        if task.mode == "work" and not (task.resource.strip() or task.assignments):
            raise ValueError(f"Task {number}: fixed work needs a resource")
        if task.earliest:
            date.fromisoformat(task.earliest)
        dependencies = [parse_dependency(pred) for pred in task.predecessors]
        if len({link.uid for link in dependencies}) != len(dependencies):
            raise ValueError(f"Task {number}: duplicate predecessors are not supported")

    result: dict[int, Scheduled] = {}
    visiting: set[int] = set()

    def calculate(uid: int) -> Scheduled:
        if uid in result:
            return result[uid]
        if uid in visiting:
            raise ValueError("Task dependencies contain a cycle")
        visiting.add(uid)
        task = tasks[uid]
        number = numbers[uid]
        if uid in children:
            for pred in task.predecessors:
                link = parse_dependency(pred)
                if link.uid not in tasks:
                    raise ValueError(f"Task {number}: predecessor internal ID {link.uid} does not exist")
                if link.kind not in ("FS", "SS"):
                    raise ValueError(f"Task {number}: a summary task supports only FS and SS dependencies, "
                                     f"not {link.kind}")
            kids = [calculate(child) for child in children[uid]]
            start, finish = min(c.start for c in kids), max(c.finish for c in kids)
            item = Scheduled(start, finish, work_between(start, finish, holidays) / 480,
                             sum(c.work for c in kids), True)
        else:
            units = total_units(task)
            if not math.isfinite(units):
                raise ValueError(f"Task {number}: total resource allocation is too large")
            rate = 8 * units / 100
            if task.mode == "work" and (rate <= 0 or not math.isfinite(rate)):
                raise ValueError(f"Task {number}: total resource allocation is outside the supported range")
            duration = task.work / rate if task.mode == "work" else task.duration
            work = task.work if task.mode == "work" else duration * 8 * units / 100
            if not math.isfinite(duration) or not math.isfinite(work):
                raise ValueError(f"Task {number}: calculated duration or work is too large")
            start = max(project_start, datetime.combine(date.fromisoformat(task.earliest), time(8)) if task.earliest else project_start)
            finish_bound = None
            # Links on enclosing summary tasks floor this task's start (FS/SS only,
            # which the summary branch has validated).
            links = [(number, parse_dependency(pred)) for pred in task.predecessors]
            ancestor = task.parent
            while ancestor:
                links += [(numbers[ancestor], parse_dependency(pred)) for pred in tasks[ancestor].predecessors]
                ancestor = tasks[ancestor].parent
            for owner, link in links:
                if link.uid not in tasks:
                    raise ValueError(f"Task {owner}: predecessor internal ID {link.uid} does not exist")
                predecessor = calculate(link.uid)
                minutes, elapsed = lag_minutes(link, predecessor.duration)
                anchor = predecessor.finish if link.kind in ("FS", "FF") else predecessor.start
                bound = offset_time(anchor, minutes, elapsed, holidays)
                if link.kind in ("FS", "SS"):
                    start = max(start, bound)
                else:
                    finish_bound = max(finish_bound, bound) if finish_bound else bound
            if not duration and finish_bound:
                start = max(start, finish_bound)
            if duration > 0:
                start = next_start(start, holidays)
            else:
                start = milestone_at_or_after(start, holidays)
            finish = add_work(start, duration * 480, holidays)
            if duration > 0 and finish_bound and finish < finish_bound:
                # For FF/SF, find the earliest start whose working-time finish
                # meets the finish constraint. Off-hours cannot be a work finish.
                candidate = subtract_work(finish_bound, duration * 480, holidays)
                start = next_start(max(start, candidate), holidays)
                finish = add_work(start, duration * 480, holidays)
                if finish < finish_bound:
                    # A finish constraint in a non-working gap, or exactly at
                    # a work-period start, needs the next representable tick.
                    target = next_start(finish_bound, holidays) + timedelta(seconds=6)
                    candidate = subtract_work(target, duration * 480, holidays)
                    start = next_start(max(start, candidate), holidays)
                    finish = add_work(start, duration * 480, holidays)
                if finish < finish_bound:
                    raise ValueError(f"Task {number}: cannot satisfy finish dependency")
            item = Scheduled(start, finish, duration, work)
        result[uid] = item
        visiting.remove(uid)
        return item

    for task in plan.tasks:
        calculate(task.uid)
    return result


def completion_percentages(plan: Plan, calculated: dict[int, Scheduled] | None = None) -> dict[int, float]:
    """Leaf progress is user-entered; summaries roll up by scheduled work."""
    calculated = calculated if calculated is not None else schedule(plan)
    children: dict[int, list[int]] = {}
    for task in plan.tasks:
        if task.parent:
            children.setdefault(task.parent, []).append(task.uid)
    result: dict[int, float] = {}
    for task in reversed(plan.tasks):
        child_ids = children.get(task.uid, [])
        if not child_ids:
            result[task.uid] = float(task.percent_complete)
            continue
        weights = [calculated[uid].work if calculated[uid].work > 0
                   else max(calculated[uid].duration, 1.0) for uid in child_ids]
        total = sum(weights)
        result[task.uid] = (sum(result[uid] * weight for uid, weight in zip(child_ids, weights)) / total
                            if total else 0.0)
    return result


def descendants(plan: Plan, uid: int) -> set[int]:
    found = {uid}
    for task in plan.tasks:
        if task.parent in found:
            found.add(task.uid)
    return found


def indent(plan: Plan, uid: int) -> None:
    index = next(i for i, t in enumerate(plan.tasks) if t.uid == uid)
    task = plan.tasks[index]
    previous = next((t for t in reversed(plan.tasks[:index]) if t.parent == task.parent), None)
    if previous is None:
        raise ValueError("Select a task with a preceding sibling to indent")
    if previous.assignments or previous.resource.strip():
        raise ValueError("Remove resource assignments from the preceding task before making it a summary")
    task.parent = previous.uid


def outdent(plan: Plan, uid: int) -> None:
    task = next(t for t in plan.tasks if t.uid == uid)
    if not task.parent:
        raise ValueError("Task is already at the top level")
    parent = next(t for t in plan.tasks if t.uid == task.parent)
    block_ids = descendants(plan, uid)
    block = [t for t in plan.tasks if t.uid in block_ids]
    remaining = [t for t in plan.tasks if t.uid not in block_ids]
    parent_subtree = descendants(plan, parent.uid) - block_ids
    insert_at = max(i for i, t in enumerate(remaining) if t.uid in parent_subtree) + 1
    task.parent = parent.parent
    plan.tasks = remaining[:insert_at] + block + remaining[insert_at:]
