"""Untrusted AI proposals, interpreted only as validated local plan edits."""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
import math
import re

from ..dependencies import Dependency, format_dependency, parse_dependency, task_numbers
from ..model import Plan, Resource, ResourceAssignment, Task, completion_percentages, descendants, schedule
from ..resources import migrate_legacy_resources, overallocation_warnings


MAX_SNAPSHOT_BYTES = 300_000
MAX_OPERATIONS = 100
SCHEMA_VERSION = "1"
FIELDS = {
    "op": {"type": "string", "enum": ["update_project", "create_task", "update_task", "move_task", "delete_task",
              "create_resource", "update_resource", "delete_resource", "assign_resource", "unassign_resource",
              "add_dependency", "remove_dependency"]},
    "target": {"type": ["string", "null"]}, "temp_id": {"type": ["string", "null"]},
    "name": {"type": ["string", "null"]}, "parent": {"type": ["string", "null"]},
    "before": {"type": ["string", "null"]}, "duration_days": {"type": ["number", "null"]},
    "earliest": {"type": ["string", "null"]}, "mode": {"type": ["string", "null"]},
    "work_hours": {"type": ["number", "null"]}, "estimated": {"type": ["boolean", "null"]},
    "resource": {"type": ["string", "null"]}, "units": {"type": ["number", "null"]},
    "capacity": {"type": ["number", "null"]}, "predecessor": {"type": ["string", "null"]},
    "kind": {"type": ["string", "null"]}, "lag": {"type": ["number", "null"]},
    "unit": {"type": ["string", "null"]}, "start_date": {"type": ["string", "null"]},
    "holidays": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]},
}
PROPOSAL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "schema_version": {"type": "string", "enum": [SCHEMA_VERSION]},
        "snapshot_revision": {"type": "string"},
        "message": {"type": "string"},
        "needs_clarification": {"type": "boolean"},
        "operations": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                       "properties": FIELDS, "required": list(FIELDS)}},
    },
    "required": ["schema_version", "snapshot_revision", "message", "needs_clarification", "operations"],
}
ALLOWED = {
    "update_project": {"name", "start_date", "holidays"},
    "create_task": {"temp_id", "name", "parent", "before", "duration_days", "earliest", "mode", "work_hours", "estimated"},
    "update_task": {"target", "name", "duration_days", "earliest", "mode", "work_hours", "estimated"},
    "move_task": {"target", "parent", "before"}, "delete_task": {"target"},
    "create_resource": {"temp_id", "name", "capacity"}, "update_resource": {"target", "name", "capacity"},
    "delete_resource": {"target"}, "assign_resource": {"target", "resource", "units"},
    "unassign_resource": {"target", "resource"},
    "add_dependency": {"target", "predecessor", "kind", "lag", "unit"},
    "remove_dependency": {"target", "predecessor"},
}


class ProposalError(ValueError):
    """A proposal is malformed, unsafe, unsupported, or no longer current."""


def _valid_unicode(value: str) -> bool:
    try:
        value.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


@dataclass(frozen=True)
class Snapshot:
    plan: Plan
    revision: str
    payload: dict


def plan_revision(plan: Plan) -> str:
    try:
        encoded = json.dumps(asdict(plan), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProposalError("The current project cannot be represented safely.") from exc
    return hashlib.sha256(encoded).hexdigest()


def make_snapshot(plan: Plan) -> Snapshot:
    baseline = copy.deepcopy(plan)
    calculated = schedule(baseline)
    progress = completion_percentages(baseline, calculated)
    revision = plan_revision(baseline)
    numbers = task_numbers(baseline.tasks)
    rows = []
    for task in baseline.tasks:
        item = calculated[task.uid]
        rows.append({
            "ref": f"task:{task.uid}", "display_id": numbers[task.uid], "name": task.name,
            "parent": f"task:{task.parent}" if task.parent else None,
            "duration_days": task.duration, "earliest_date": task.earliest or None,
            "start_rule": task.start_mode, "mode": task.mode, "work_hours": task.work, "estimated": task.estimated,
            "percent_complete": task.percent_complete,
            "collapsed_summary_color": task.summary_color or None,
            "assignments": [{"resource": f"resource:{a.resource_uid}", "units_percent": a.units} for a in task.assignments],
            "legacy_resource": task.resource or None, "legacy_units_percent": task.units if task.resource else None,
            "predecessors": [{"task": f"task:{link.uid}", "kind": link.kind, "lag": link.lag, "unit": link.unit}
                             for link in map(parse_dependency, task.predecessors)],
            "calculated": {"start": item.start.isoformat(), "finish": item.finish.isoformat(),
                           "duration_days": item.duration, "work_hours": item.work, "summary": item.summary,
                           "percent_complete": progress[task.uid]},
        })
    normalized = copy.deepcopy(baseline)
    migrate_legacy_resources(normalized)
    resource_names = {resource.uid: resource.name for resource in normalized.resources}
    stable_resource_ids = {resource.uid for resource in baseline.resources}
    warnings = [{"resource": f"resource:{uid}" if uid in stable_resource_ids else None,
                 "resource_name": resource_names.get(uid, "Unknown"),
                 "intervals": [{"start": value.start.isoformat(), "finish": value.finish.isoformat(),
                                "units_percent": value.units, "capacity_percent": value.capacity} for value in values]}
                for uid, values in overallocation_warnings(normalized).items()]
    payload = {
        "schema_version": SCHEMA_VERSION, "snapshot_revision": revision,
        "project": {"name": baseline.title, "start_date": baseline.start, "holidays": list(baseline.holidays)},
        "calendar": {"workdays": "Monday-Friday", "hours": "08:00-12:00, 13:00-17:00", "working_hours_per_day": 8},
        "tasks": rows,
        "resources": [{"ref": f"resource:{r.uid}", "name": r.name, "capacity_percent": r.max_units}
                      for r in baseline.resources],
        "overallocation_warnings": warnings,
        "limits": {"max_operations": MAX_OPERATIONS, "summary_fields_are_calculated": True,
                   "resource_levelling": False, "earliest_start_is_date_only": True},
    }
    if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ProposalError("The project is too large to send safely; no data was sent.")
    return Snapshot(baseline, revision, payload)


def parse_proposal(text: str, revision: str) -> dict:
    if not isinstance(text, str) or len(text) > 500_000:
        raise ProposalError("The model response is missing or too large.")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProposalError("The model returned duplicate JSON fields.")
            result[key] = value
        return result
    def invalid_constant(_value):
        raise ProposalError("The model returned an invalid number.")
    try:
        result = json.loads(text, object_pairs_hook=unique, parse_constant=invalid_constant)
    except ProposalError:
        raise
    except (ValueError, OverflowError, RecursionError, TypeError) as exc:
        raise ProposalError("The model did not return a valid proposal.") from exc
    if not isinstance(result, dict) or set(result) != set(PROPOSAL_SCHEMA["required"]):
        raise ProposalError("The model returned an unexpected proposal structure.")
    if result["schema_version"] != SCHEMA_VERSION or result["snapshot_revision"] != revision:
        raise ProposalError("The model response does not match the project snapshot.")
    if (not isinstance(result["message"], str) or len(result["message"]) > 4000
            or not _valid_unicode(result["message"])):
        raise ProposalError("The model's explanation is invalid or too long.")
    if type(result["needs_clarification"]) is not bool or not isinstance(result["operations"], list):
        raise ProposalError("The model response has invalid fields.")
    if len(result["operations"]) > MAX_OPERATIONS:
        raise ProposalError(f"A proposal may contain at most {MAX_OPERATIONS} operations.")
    if result["needs_clarification"] and result["operations"]:
        raise ProposalError("A clarification request cannot also change the project.")
    for operation in result["operations"]:
        if not isinstance(operation, dict) or set(operation) != set(FIELDS):
            raise ProposalError("An operation has missing or unexpected fields.")
        name = operation.get("op")
        if not isinstance(name, str) or name not in ALLOWED:
            raise ProposalError("The model requested an unsupported operation.")
        for field, value in operation.items():
            if field == "op" or value is None:
                continue
            if field not in ALLOWED[name]:
                raise ProposalError(f"{name}: unexpected field {field}.")
            spec = FIELDS[field]
            if field == "holidays":
                if (not isinstance(value, list) or len(value) > 1000
                        or any(not isinstance(v, str) or len(v) > 32 or not _valid_unicode(v) for v in value)):
                    raise ProposalError("Invalid holiday list.")
            elif "string" in spec.get("type", []):
                if not isinstance(value, str) or len(value) > 2000 or not _valid_unicode(value):
                    raise ProposalError(f"{name}: invalid {field}.")
            elif "boolean" in spec.get("type", []):
                if type(value) is not bool:
                    raise ProposalError(f"{name}: invalid {field}.")
            elif "number" in spec.get("type", []):
                try:
                    valid_number = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
                except OverflowError:
                    valid_number = False
                if not valid_number:
                    raise ProposalError(f"{name}: invalid {field}.")
    return result


def _date(value: str, allow_empty=False) -> str:
    if allow_empty and value == "":
        return ""
    try:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("Invalid date")
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise ProposalError("Dates must use YYYY-MM-DD; time-of-day constraints are not supported.") from exc


def _number(value, label, minimum, maximum):
    try:
        valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and minimum <= value <= maximum
    except OverflowError:
        valid = False
    if not valid:
        raise ProposalError(f"{label} must be between {minimum:g} and {maximum:g}.")
    return float(value)


def _name(value, label):
    if not isinstance(value, str) or not value.strip() or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ProposalError(f"{label} needs a non-empty, single-line name.")
    return value.strip()


def _temp(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(prefix + r":[A-Za-z0-9_-]{1,40}", value):
        raise ProposalError(f"New items need a unique {prefix}: identifier.")
    return value


def _insert(plan: Plan, block: list[Task], parent: int, before: int | None):
    by_id = {task.uid: task for task in plan.tasks}
    if parent:
        if parent not in by_id:
            raise ProposalError("The requested parent task does not exist.")
        parent_task = by_id[parent]
        if (parent_task.assignments or parent_task.resource.strip()) and not any(task.parent == parent for task in plan.tasks):
            raise ProposalError("Remove the parent task's resource assignments before adding children.")
    if before is not None:
        if before not in by_id or by_id[before].parent != parent:
            raise ProposalError("The insertion point must be a sibling of the new task.")
        index = next(i for i, task in enumerate(plan.tasks) if task.uid == before)
    elif parent:
        subtree = descendants(plan, parent)
        index = max(i for i, task in enumerate(plan.tasks) if task.uid in subtree) + 1
    else:
        index = len(plan.tasks)
    block[0].parent = parent
    plan.tasks[index:index] = block


def apply_operations(snapshot: Snapshot, proposal: dict, *, reserved_task_ids=(), reserved_resource_ids=()) -> Plan:
    """Apply a validated batch to a copy; never touch the live working plan."""
    try:
        proposal = parse_proposal(json.dumps(proposal, allow_nan=False), snapshot.revision)
    except (TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, ProposalError):
            raise
        raise ProposalError("The proposal cannot be represented safely.") from exc
    candidate = copy.deepcopy(snapshot.plan)
    new_tasks, new_resources = {}, {}
    original_tasks = {task.uid for task in snapshot.plan.tasks}
    original_resources = {resource.uid for resource in snapshot.plan.resources}
    next_task_uid = max(original_tasks | set(reserved_task_ids), default=0) + 1
    next_resource_uid = max(original_resources | set(reserved_resource_ids), default=0) + 1
    touched_calculated = set()
    generated_durations = set()

    def task_ref(ref):
        if isinstance(ref, str) and ref in new_tasks:
            uid = new_tasks[ref]
        elif isinstance(ref, str) and re.fullmatch(r"task:[1-9][0-9]*", ref):
            uid = int(ref[5:])
            if uid not in original_tasks:
                raise ProposalError("A task reference was not present in the snapshot.")
        else:
            raise ProposalError("Invalid task reference; create new items before using them.")
        if not any(task.uid == uid for task in candidate.tasks):
            raise ProposalError("A referenced task no longer exists.")
        return uid

    def resource_ref(ref):
        if isinstance(ref, str) and ref in new_resources:
            uid = new_resources[ref]
        elif isinstance(ref, str) and re.fullmatch(r"resource:[1-9][0-9]*", ref):
            uid = int(ref[9:])
            if uid not in original_resources:
                raise ProposalError("A resource reference was not present in the snapshot.")
        else:
            raise ProposalError("Invalid resource reference; create new items before using them.")
        if not any(resource.uid == uid for resource in candidate.resources):
            raise ProposalError("A referenced resource no longer exists.")
        return uid

    for operation in proposal["operations"]:
        kind = operation["op"]
        if kind == "update_project":
            if all(operation[field] is None for field in ("name", "start_date", "holidays")):
                raise ProposalError("The project update has no changes.")
            if operation["name"] is not None:
                candidate.title = _name(operation["name"], "Project")
            if operation["start_date"] is not None:
                candidate.start = _date(operation["start_date"])
            if operation["holidays"] is not None:
                holidays = [_date(day) for day in operation["holidays"]]
                if len(set(holidays)) != len(holidays):
                    raise ProposalError("Holiday dates must be unique.")
                candidate.holidays = holidays
        elif kind == "create_task":
            temp = _temp(operation["temp_id"], "new-task")
            if temp in new_tasks:
                raise ProposalError("New task references must be unique.")
            uid = next_task_uid
            next_task_uid += 1
            mode = operation["mode"] or "duration"
            if mode not in ("duration", "work"):
                raise ProposalError("Task mode must be duration or work.")
            if (mode == "work" and operation["duration_days"] is not None) or (mode != "work" and operation["work_hours"] is not None):
                raise ProposalError("Supply only the editable work or duration for the selected task mode.")
            task = Task(uid, _name(operation["name"], "Task"), mode=mode)
            if operation["duration_days"] is not None:
                task.duration = _number(operation["duration_days"], "Duration", 0, 100000)
            if operation["work_hours"] is not None:
                task.work = _number(operation["work_hours"], "Work", 0, 800000)
            if mode == "work" and operation["work_hours"] is None:
                raise ProposalError("A fixed-work task needs an explicit amount of work.")
            if operation["earliest"] is not None:
                task.earliest = _date(operation["earliest"], allow_empty=True)
            if operation["estimated"] is not None:
                task.estimated = operation["estimated"]
            parent = task_ref(operation["parent"]) if operation["parent"] else 0
            before = task_ref(operation["before"]) if operation["before"] else None
            _insert(candidate, [task], parent, before)
            new_tasks[temp] = uid
            generated_durations.add(uid)
            if any(operation[field] is not None for field in ("duration_days", "earliest", "mode", "work_hours")):
                touched_calculated.add(uid)
        elif kind == "update_task":
            uid = task_ref(operation["target"])
            task = next(task for task in candidate.tasks if task.uid == uid)
            previous_duration, previous_work, previous_mode, previous_estimate = (
                task.duration, task.work, task.mode, task.estimated)
            fields = ("name", "duration_days", "earliest", "mode", "work_hours", "estimated")
            if all(operation[field] is None for field in fields):
                raise ProposalError("The task update has no changes.")
            if operation["name"] is not None:
                task.name = _name(operation["name"], "Task")
            # The assistant may only choose duration or work, but a task already in fixed-units
            # mode (from an MPP import) keeps that mode, with duration as its editable value.
            new_mode = operation["mode"] if operation["mode"] is not None else task.mode
            if new_mode not in ("duration", "work") and not (new_mode == "units" and operation["mode"] is None):
                raise ProposalError("Task mode must be duration or work.")
            if new_mode != task.mode and ((new_mode == "work" and operation["work_hours"] is None)
                                          or (new_mode == "duration" and operation["duration_days"] is None)):
                raise ProposalError("Changing task mode requires an explicit work or duration value.")
            if operation["duration_days"] is not None:
                if new_mode == "work":
                    raise ProposalError("Duration is calculated for fixed-work tasks.")
                task.duration = _number(operation["duration_days"], "Duration", 0, 100000)
            if operation["work_hours"] is not None:
                if new_mode != "work":
                    raise ProposalError("Work is calculated for fixed-duration and fixed-units tasks.")
                task.work = _number(operation["work_hours"], "Work", 0, 800000)
            if ((operation["duration_days"] is not None and task.duration != previous_duration)
                    or (operation["work_hours"] is not None and task.work != previous_work)
                    or new_mode != previous_mode):
                generated_durations.add(uid)
            task.mode = new_mode
            if operation["earliest"] is not None:
                task.earliest = _date(operation["earliest"], allow_empty=True)
                if not task.earliest:
                    task.start_mode = "no_earlier"  # Clearing the date releases a fixed start.
            if operation["estimated"] is not None:
                task.estimated = operation["estimated"]
                if (operation["estimated"] and not previous_estimate
                        and (operation["duration_days"] is not None or operation["work_hours"] is not None)):
                    task.estimated = False
            if any(operation[field] is not None for field in ("duration_days", "earliest", "mode", "work_hours")):
                touched_calculated.add(uid)
        elif kind == "move_task":
            uid = task_ref(operation["target"])
            parent = task_ref(operation["parent"]) if operation["parent"] else 0
            before = task_ref(operation["before"]) if operation["before"] else None
            subtree = descendants(candidate, uid)
            if parent in subtree or before in subtree:
                raise ProposalError("A task cannot be moved inside its own subtree.")
            block = [task for task in candidate.tasks if task.uid in subtree]
            candidate.tasks = [task for task in candidate.tasks if task.uid not in subtree]
            _insert(candidate, block, parent, before)
        elif kind == "delete_task":
            uid = task_ref(operation["target"])
            removed = descendants(candidate, uid)
            candidate.tasks = [task for task in candidate.tasks if task.uid not in removed]
            for task in candidate.tasks:
                task.predecessors = [value for value in task.predecessors if parse_dependency(value).uid not in removed]
        elif kind == "create_resource":
            temp = _temp(operation["temp_id"], "new-resource")
            if temp in new_resources:
                raise ProposalError("New resource references must be unique.")
            uid = next_resource_uid
            next_resource_uid += 1
            capacity = 100.0 if operation["capacity"] is None else _number(operation["capacity"], "Capacity", 0, 10000)
            candidate.resources.append(Resource(uid, _name(operation["name"], "Resource"), capacity))
            new_resources[temp] = uid
        elif kind == "update_resource":
            uid = resource_ref(operation["target"])
            resource = next(resource for resource in candidate.resources if resource.uid == uid)
            if operation["name"] is None and operation["capacity"] is None:
                raise ProposalError("The resource update has no changes.")
            if operation["name"] is not None:
                if any(task.resource.strip() == resource.name for task in candidate.tasks):
                    raise ProposalError("Legacy resource assignments must be migrated before renaming that resource.")
                resource.name = _name(operation["name"], "Resource")
            if operation["capacity"] is not None:
                resource.max_units = _number(operation["capacity"], "Capacity", 0, 10000)
        elif kind == "delete_resource":
            uid = resource_ref(operation["target"])
            resource = next(resource for resource in candidate.resources if resource.uid == uid)
            if any(any(a.resource_uid == uid for a in task.assignments) or task.resource.strip() == resource.name
                   for task in candidate.tasks):
                raise ProposalError("Remove resource assignments before deleting the resource.")
            candidate.resources = [resource for resource in candidate.resources if resource.uid != uid]
        elif kind in ("assign_resource", "unassign_resource"):
            uid, resource_uid = task_ref(operation["target"]), resource_ref(operation["resource"])
            task = next(task for task in candidate.tasks if task.uid == uid)
            if task.resource.strip():
                raise ProposalError("Legacy resource assignments must be migrated before AI changes them.")
            existing = next((a for a in task.assignments if a.resource_uid == resource_uid), None)
            if kind == "unassign_resource":
                if existing is None:
                    raise ProposalError("The task does not have that resource assignment.")
                task.assignments.remove(existing)
            else:
                if any(other.parent == uid for other in candidate.tasks):
                    raise ProposalError("Resources cannot be assigned to summary tasks.")
                units = _number(operation["units"], "Allocation", 0.0000000001, 10000)
                if existing:
                    existing.units = units
                else:
                    task.assignments.append(ResourceAssignment(resource_uid, units))
        elif kind in ("add_dependency", "remove_dependency"):
            uid, predecessor = task_ref(operation["target"]), task_ref(operation["predecessor"])
            task = next(task for task in candidate.tasks if task.uid == uid)
            existing = next((value for value in task.predecessors if parse_dependency(value).uid == predecessor), None)
            if kind == "remove_dependency":
                if existing is None:
                    raise ProposalError("The requested dependency does not exist.")
                task.predecessors.remove(existing)
            else:
                if existing is not None:
                    raise ProposalError("Duplicate dependencies are not supported.")
                dependency_type = operation["kind"] or "FS"
                unit = operation["unit"] or "d"
                lag = 0.0 if operation["lag"] is None else _number(operation["lag"], "Lag", -100000, 100000)
                if dependency_type not in ("FS", "SS", "FF", "SF") or unit not in ("m", "h", "d", "w", "mo", "em", "eh", "ed", "ew", "emo", "%"):
                    raise ProposalError("Unsupported dependency type or lag unit.")
                task.predecessors.append(format_dependency(Dependency(predecessor, dependency_type, lag, unit)))
    # Durations generated by AI are confirmed by default, even if the model
    # marks them estimated in the same batch. Existing unrelated estimates stay intact.
    for task in candidate.tasks:
        if task.uid in generated_durations:
            task.estimated = False
    for uid in touched_calculated:
        if any(task.parent == uid for task in candidate.tasks):
            raise ProposalError("Summary task dates, duration, mode and work are calculated.")
    try:
        schedule(candidate)
        overallocation_warnings(candidate)
    except (ValueError, KeyError, OverflowError, RecursionError) as exc:
        raise ProposalError(f"The proposed project is invalid: {exc}") from exc
    return candidate
