"""Session-only row clipboard and detached, validated paste operations."""
from __future__ import annotations

import copy
from dataclasses import dataclass
import uuid
import weakref

from PySide6.QtCore import QMimeData
from PySide6.QtWidgets import QApplication

from .ai.proposals import plan_revision
from .dependencies import parse_dependency, remap_predecessors
from .model import Plan, Resource, descendants, schedule


MIME = "application/x-onepyproject-rows"
_payload = None


@dataclass
class Rows:
    token: str
    tasks: list
    roots: list[int]
    resources: dict
    source: object
    source_plan: Plan
    source_revision: str
    source_tasks: dict
    cut: bool


def put_rows(window, selection, cut=False):
    """Copy selected rows and complete selected subtrees to the OS clipboard."""
    global _payload
    selected = set(selection)
    if not selected:
        return False
    included = set()
    for uid in selected:
        included.update(descendants(window.plan, uid))
    tasks = [task for task in window.plan.tasks if task.uid in included]
    roots = [task.uid for task in tasks if task.parent not in included]
    payload = Rows(uuid.uuid4().hex, copy.deepcopy(tasks), roots,
                   {resource.uid: copy.deepcopy(resource) for resource in window.plan.resources},
                   weakref.ref(window), window.plan, plan_revision(window.plan),
                   {task.uid: task for task in window.plan.tasks}, cut)
    mime = QMimeData()
    mime.setData(MIME, payload.token.encode("ascii"))
    mime.setText("\n".join("\t".join(window.items[task.uid].text(col) for col in range(9)) for task in tasks))
    _payload = payload
    QApplication.clipboard().setMimeData(mime)
    return True


def get_rows():
    mime = QApplication.clipboard().mimeData()
    if _payload is None or mime is None or not mime.hasFormat(MIME):
        return None
    try:
        return _payload if bytes(mime.data(MIME)).decode("ascii") == _payload.token else None
    except (UnicodeError, ValueError):
        return None


def clear_cut(rows):
    """A cut is a one-time move; invalidate it only after a successful paste."""
    global _payload
    if _payload is rows and rows.cut:
        if get_rows() is rows:
            clipboard = QApplication.clipboard()
            clipboard.setText(clipboard.text())
        _payload = None


def release_rows():
    """Keep copied text, but release session-only Qt MIME data before shutdown."""
    global _payload
    if get_rows() is not None:
        clipboard = QApplication.clipboard()
        text = clipboard.text()
        clipboard.setText(text)
    _payload = None


def paste_rows(window, rows, anchor):
    """Return a candidate plan, inserted IDs and the number of omitted links."""
    same_source = rows.source() is window and window.plan is rows.source_plan
    if rows.cut and (not same_source or plan_revision(window.plan) != rows.source_revision):
        raise ValueError("The cut rows must be pasted into the unchanged source project. Copy them again if it changed.")
    moving = {task.uid for task in rows.tasks}
    if rows.cut and anchor in moving:
        raise ValueError("Choose a destination outside the cut rows.")
    candidate = copy.deepcopy(window.plan)
    original = {task.uid: task for task in window.plan.tasks}
    candidate_by_uid = {task.uid: task for task in candidate.tasks}
    destination = candidate_by_uid.get(anchor)
    parent = destination.parent if destination else 0
    if rows.cut:
        copies = [task for task in candidate.tasks if task.uid in moving]
        candidate.tasks = [task for task in candidate.tasks if task.uid not in moving]
        mapping = {uid: uid for uid in moving}
    else:
        copies = copy.deepcopy(rows.tasks)
        next_uid = max(original, default=0) + 1
        mapping = {task.uid: next_uid + index for index, task in enumerate(copies)}
        resource_map = {}
        by_name = {resource.name: resource.uid for resource in candidate.resources}
        next_resource = max((resource.uid for resource in candidate.resources), default=0) + 1
        for task in copies:
            for assignment in task.assignments:
                old_uid = assignment.resource_uid
                if old_uid not in resource_map:
                    resource = rows.resources.get(old_uid)
                    if resource is None:
                        raise ValueError("A copied resource is unavailable.")
                    if same_source and any(r.uid == old_uid and r.name == resource.name for r in candidate.resources):
                        resource_map[old_uid] = old_uid
                    elif resource.name in by_name:
                        resource_map[old_uid] = by_name[resource.name]
                    else:
                        resource_map[old_uid] = next_resource
                        candidate.resources.append(Resource(next_resource, resource.name, resource.max_units))
                        by_name[resource.name] = next_resource
                        next_resource += 1
                assignment.resource_uid = resource_map[old_uid]
    original_links = {}
    for task in copies:
        old_uid = task.uid
        original_links[mapping[old_uid]] = list(task.predecessors)
        task.uid = mapping[old_uid]
        task.parent = parent if old_uid in rows.roots else mapping[task.parent]
        task.predecessors = []
    if destination:
        subtree = descendants(candidate, anchor)
        position = max(i for i, task in enumerate(candidate.tasks) if task.uid in subtree) + 1
    else:
        position = len(candidate.tasks)
    candidate.tasks[position:position] = copies
    schedule(candidate)
    summaries = {task.parent for task in candidate.tasks if task.parent}
    dropped = 0
    for task in copies:
        for raw in original_links[task.uid]:
            uid = parse_dependency(raw).uid
            if uid in mapping:
                target = mapping[uid]
            elif same_source and uid in original and original[uid] == rows.source_tasks.get(uid):
                target = uid
            else:
                dropped += 1
                continue
            if task.uid in summaries or target in summaries or target == task.uid:
                dropped += 1
                continue
            task.predecessors.append(remap_predecessors([raw], {uid: target})[0])
            try:
                schedule(candidate)
            except (ValueError, KeyError, OverflowError, RecursionError):
                task.predecessors.pop()
                dropped += 1
    schedule(candidate)
    return candidate, [mapping[uid] for uid in rows.roots], dropped
