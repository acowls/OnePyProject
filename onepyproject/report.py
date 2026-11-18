"""Read-only schedule snapshots shared by the HTML and PDF exporters."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
import os
from pathlib import Path
import tempfile
from typing import Callable

from .model import MODE_LABELS, Plan, completion_percentages, estimated_tasks, schedule
from .dependencies import remap_predecessors, task_numbers
from .resources import format_task_resources


@dataclass(frozen=True)
class ReportRow:
    uid: int
    parent: int
    depth: int
    name: str
    duration: str
    start: datetime
    finish: datetime
    predecessors: str
    resources: str
    work: str
    mode: str
    summary: bool
    percent_complete: str


@dataclass(frozen=True)
class Report:
    title: str
    project_start: str
    rows: tuple[ReportRow, ...]
    timeline_start: datetime | None
    timeline_finish: datetime | None


def make_report(plan: Plan) -> Report:
    """Calculate a snapshot without changing the working plan."""
    calculated = schedule(plan)
    progress = completion_percentages(plan, calculated)
    estimated = estimated_tasks(plan)
    numbers = task_numbers(plan.tasks)
    depths = {}
    rows = []
    for task in plan.tasks:
        item = calculated[task.uid]
        depth = depths.get(task.parent, -1) + 1
        depths[task.uid] = depth
        rows.append(ReportRow(
            uid=numbers[task.uid], parent=numbers[task.parent] if task.parent else 0, depth=depth, name=task.name,
            duration=f"{item.duration:g} d{'?' if estimated[task.uid] else ''}",
            start=item.start, finish=item.finish,
            predecessors=", ".join(map(str, remap_predecessors(task.predecessors, numbers))),
            resources=format_task_resources(plan, task),
            work=f"{item.work:g} h", mode=MODE_LABELS[task.mode],
            summary=item.summary, percent_complete=f"{progress[task.uid]:.1f}%",
        ))
    return Report(plan.title, plan.start, tuple(rows),
                  min((row.start for row in rows), default=None),
                  max((row.finish for row in rows), default=None))


def format_report_time(value: datetime, show_time: bool = False) -> str:
    if not show_time:
        return value.date().isoformat()
    # Preserve sub-minute lag calculations rather than silently rounding them.
    precision = "minutes" if not value.second and not value.microsecond else "auto"
    return value.isoformat(sep=" ", timespec=precision)


def timeline_positions(report: Report, row: ReportRow) -> tuple[float, float]:
    """Positions from 0 to 1 on a scale covering every task, including milestones."""
    if report.timeline_start is None or report.timeline_finish is None:
        return 0.0, 0.0
    span = (report.timeline_finish - report.timeline_start).total_seconds()
    if not span:
        return 0.5, 0.5
    start = (row.start - report.timeline_start).total_seconds() / span
    finish = (row.finish - report.timeline_start).total_seconds() / span
    return max(0.0, min(1.0, start)), max(0.0, min(1.0, finish))


def write_atomically(path: str | Path, write: Callable[[Path], None]) -> None:
    """Replace only after a complete write, leaving existing files on failure."""
    target = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".onepyproject-report-", suffix=".tmp", dir=target.parent)
    os.close(fd)
    try:
        write(Path(temporary))
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _svg_timeline(report: Report, row: ReportRow) -> str:
    start, finish = timeline_positions(report, row)
    # Leave room for milestone diamonds and summary end caps at both edges.
    x1, x2 = 8 + 984 * start, 8 + 984 * finish
    grid = ''.join(f'<line x1="{x}" y1="0" x2="{x}" y2="24" class="grid"/>'
                   for x in (8, 254, 500, 746, 992))
    if row.summary:
        shape = (f'<path d="M {x1:.3f} 7 H {x2:.3f} M {x1:.3f} 7 v 9 '
                 f'M {x2:.3f} 7 v 9" class="summary-bar"/>')
    elif row.start == row.finish:
        shape = ''
    else:
        shape = f'<rect x="{x1:.3f}" y="6" width="{max(2.0, x2 - x1):.3f}" height="12" rx="2" class="bar"/>'
    milestone = (f'<span class="milestone" style="left:{x1 / 10:.3f}%" aria-hidden="true"></span>'
                 if not row.summary and row.start == row.finish else '')
    return (f'<div class="gantt-plot"><svg viewBox="0 0 1000 24" preserveAspectRatio="none" role="img" '
            f'aria-label="Timeline for task {row.uid}">{grid}{shape}</svg>{milestone}</div>')


def html_report(report: Report, show_time: bool = False) -> str:
    """Create one offline HTML document. Project text is always escaped."""
    title = escape(report.title)
    range_label = (f"{report.timeline_start.date().isoformat()} to {report.timeline_finish.date().isoformat()}"
                   if report.timeline_start is not None else "No scheduled tasks")
    rows = []
    for row in report.rows:
        values = (str(row.uid), row.duration, format_report_time(row.start, show_time), format_report_time(row.finish, show_time),
                  row.predecessors, row.resources, row.work, row.mode, row.percent_complete)
        indent = min(row.depth, 20) * 16
        task_label = escape(row.name)
        parent_label = f'<span class="parent">Parent: {row.parent}</span>' if row.parent else ''
        rows.append(
            f'<tr class="{"summary" if row.summary else "task"}">'
            f'<td>{escape(values[0])}</td><td class="name" style="padding-left:{8 + indent}px">'
            f'{task_label}{parent_label}</td>'
            + ''.join(f'<td>{escape(value)}</td>' for value in values[1:4])
            + f'<td class="timeline">{_svg_timeline(report, row)}</td>'
            + ''.join(f'<td>{escape(value)}</td>' for value in values[4:])
            + '</tr>'
        )
    body = '\n'.join(rows) if rows else '<tr><td colspan="11" class="empty">No tasks in this project.</td></tr>'
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — OnePyProject</title>
<style>
body{{font:14px system-ui,sans-serif;color:#24354a;background:#fff;margin:24px}}
h1{{font-size:22px;margin:0 0 6px;overflow-wrap:anywhere}} p{{color:#526779;margin:5px 0}}
.table-wrap{{overflow-x:auto;margin-top:18px}} table{{border-collapse:collapse;width:100%;min-width:1450px}}
th,td{{border-bottom:1px solid #dfe6ed;padding:8px;text-align:left;vertical-align:middle;overflow-wrap:anywhere}}
th{{background:#edf3f7;color:#304d65;font-size:12px;white-space:nowrap}} tr:nth-child(even){{background:#f8fafc}}
th:nth-child(1){{min-width:35px}} th:nth-child(2){{min-width:200px}} th:nth-child(3){{min-width:72px}}
th:nth-child(4),th:nth-child(5){{min-width:120px}} th:nth-child(6){{min-width:350px}}
th:nth-child(7){{min-width:105px}} th:nth-child(8){{min-width:135px}}
th:nth-child(9){{min-width:50px}} th:nth-child(10){{min-width:80px}} th:nth-child(11){{min-width:80px}}
.summary td{{font-weight:600}} .name{{min-width:200px;white-space:pre-wrap}}
.parent{{display:block;color:#6a7d8b;font-size:11px;font-weight:normal}} .timeline{{width:28%;min-width:350px}}
.gantt-plot{{position:relative}} svg{{display:block;width:100%;height:24px}} .grid{{stroke:#e2e8ef;stroke-width:1}}
.bar{{fill:#72a9e0}} .summary-bar{{fill:none;stroke:#343c49;stroke-width:3}}
.milestone{{position:absolute;top:50%;width:10px;height:10px;background:#344d69;transform:translate(-50%,-50%) rotate(45deg)}}
.empty{{text-align:center;color:#6a7d8b}} @media print{{body{{margin:0}} .table-wrap{{overflow:visible}}}}
</style></head><body>
<h1>{title}</h1><p>Project start: {escape(report.project_start)} · Read-only schedule snapshot</p>
<p>Dates and times are local. ? indicates an estimate. Dependencies are listed, not drawn. Scroll horizontally to see all columns.</p>
<div class="table-wrap"><table><thead><tr><th>ID</th><th>Task</th><th>Duration</th><th>Start</th><th>Finish</th><th>Gantt<br>{escape(range_label)}</th><th>Predecessors</th><th>Resources</th><th>Work</th><th>Mode</th><th>Complete</th></tr></thead>
<tbody>{body}</tbody></table></div></body></html>\n'''


def export_html(plan: Plan, path: str | Path, show_time: bool = False) -> None:
    document = html_report(make_report(plan), show_time)
    write_atomically(path, lambda temporary: temporary.write_text(document, encoding="utf-8"))
