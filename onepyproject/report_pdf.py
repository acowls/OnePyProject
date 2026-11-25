"""Render a paginated landscape PDF using Qt's built-in PDF writer."""
from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QMarginsF, QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QPageLayout, QPageSize, QPainter,
                           QPdfWriter, QPen, QPolygonF, QTextLayout, QTextOption)

from .model import Plan
from .report import Report, ReportRow, format_report_time, make_report, timeline_positions, write_atomically


PAPER_SIZES = {"A4": QPageSize.PageSizeId.A4, "A3": QPageSize.PageSizeId.A3}
COLUMN_LABELS = ("ID", "Task / parent", "Duration", "Start", "Finish", "Pred.", "Resources", "Work", "Mode", "Complete", "Gantt")
COLUMN_WIDTHS = (0.035, 0.18, 0.055, 0.095, 0.095, 0.065, 0.11, 0.05, 0.07, 0.06, 0.185)


def _text_layout(text: str, font: QFont, width: float, line_height: float) -> tuple[QTextLayout, int]:
    layout = QTextLayout(text.replace("\n", "\u2028"), font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    layout.beginLayout()
    lines = 0
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(max(1.0, width))
        line.setPosition(QPointF(0, lines * line_height))
        lines += 1
    layout.endLayout()
    return layout, max(lines, 1)


def _row_values(row: ReportRow, show_time: bool) -> tuple[str, ...]:
    name = row.name + (f" (parent {row.parent})" if row.parent else "")
    return (str(row.uid), name, row.duration, format_report_time(row.start, show_time),
            format_report_time(row.finish, show_time), row.predecessors, row.resources, row.work, row.mode,
            row.percent_complete)


def _draw_timeline(painter: QPainter, report: Report, row: ReportRow, rect: QRectF) -> None:
    painter.save()
    painter.setClipRect(rect)
    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        x = rect.left() + 5 + fraction * max(1, rect.width() - 10)
        painter.setPen(QPen(QColor("#e2e8ef"), 0.5))
        painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
    start, finish = timeline_positions(report, row)
    x1 = rect.left() + 5 + start * max(1, rect.width() - 10)
    x2 = rect.left() + 5 + finish * max(1, rect.width() - 10)
    center = rect.center().y()
    if row.summary:
        painter.setPen(QPen(QColor("#343c49"), 1.6))
        painter.drawLine(QPointF(x1, center - 2), QPointF(x2, center - 2))
        painter.drawLine(QPointF(x1, center - 2), QPointF(x1, center + 4))
        painter.drawLine(QPointF(x2, center - 2), QPointF(x2, center + 4))
    elif row.start == row.finish:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#344d69"))
        painter.drawPolygon(QPolygonF([QPointF(x1, center - 4), QPointF(x1 + 4, center),
                                       QPointF(x1, center + 4), QPointF(x1 - 4, center)]))
    else:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#72a9e0"))
        painter.drawRoundedRect(QRectF(x1, center - 4, max(1.5, x2 - x1), 8), 1.5, 1.5)
    painter.restore()


def _render_pdf(report: Report, path: Path, paper: str, show_time: bool) -> None:
    writer = QPdfWriter(str(path))
    writer.setResolution(72)
    page_layout = QPageLayout(QPageSize(PAPER_SIZES[paper]), QPageLayout.Orientation.Landscape,
                              QMarginsF(10, 10, 10, 10), QPageLayout.Unit.Millimeter)
    if not writer.setPageLayout(page_layout):
        raise OSError(f"Could not set {paper} landscape page size")
    writer.setTitle(report.title)
    writer.setCreator("OnePyProject")
    painter = QPainter()
    if not painter.begin(writer):
        raise OSError("Could not create the PDF")
    try:
        width, height = float(writer.width()), float(writer.height())
        widths = [width * fraction for fraction in COLUMN_WIDTHS]
        positions = [0.0]
        for column_width in widths:
            positions.append(positions[-1] + column_width)
        # Arial is available on the supported desktop targets; unlike the
        # generic macOS fallback it preserves punctuation when text is copied.
        title_font = QFont("Arial")
        title_font.setPointSizeF(13)
        title_font.setBold(True)
        detail_font = QFont("Arial")
        detail_font.setPointSizeF(7.0)
        header_font = QFont(detail_font)
        header_font.setBold(True)
        row_font = QFont("Arial")
        row_font.setPointSizeF(7.5)
        page = 0
        content_top = 73.0
        bottom = height - 15.0

        def header() -> float:
            nonlocal page
            page += 1
            painter.setPen(QColor("#24354a"))
            painter.setFont(title_font)
            title = QFontMetricsF(title_font).elidedText(report.title, Qt.TextElideMode.ElideRight, width - 5)
            painter.drawText(QRectF(0, 0, width, 21), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), title)
            painter.setFont(detail_font)
            painter.setPen(QColor("#526779"))
            painter.drawText(QRectF(0, 23, width, 12), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             f"Start: {report.project_start}   •   Read-only snapshot   •   Dates and times are local")
            painter.drawText(QRectF(0, 35, width, 12), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             "? = estimate. Dependencies are listed, not drawn. Timeline is scaled to the full project.")
            painter.fillRect(QRectF(0, 50, width, 23), QColor("#edf3f7"))
            painter.setFont(header_font)
            painter.setPen(QColor("#304d65"))
            for index, label in enumerate(COLUMN_LABELS):
                painter.drawText(QRectF(positions[index] + 2, 51, widths[index] - 4, 10),
                                 int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), label)
            if report.timeline_start is not None:
                painter.setFont(detail_font)
                left, chart_width = positions[-2], widths[-1]
                painter.drawText(QRectF(left + 2, 62, chart_width / 2 - 2, 9),
                                 int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                                 report.timeline_start.date().isoformat())
                painter.drawText(QRectF(left + chart_width / 2, 62, chart_width / 2 - 2, 9),
                                 int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                                 report.timeline_finish.date().isoformat())
            painter.setFont(detail_font)
            painter.setPen(QColor("#526779"))
            painter.drawText(QRectF(0, height - 12, width, 10),
                             int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), f"Page {page}")
            return content_top

        def next_page() -> float:
            if not writer.newPage():
                raise OSError("Could not add a PDF page")
            return header()

        y = header()
        # Keep the header compact on every page, but never lose a long title.
        if QFontMetricsF(title_font).horizontalAdvance(report.title) > width - 5:
            line_height = math.ceil(QFontMetricsF(row_font).height() + 1)
            title_layout, total_lines = _text_layout("Full project title: " + report.title, row_font, width - 6, line_height)
            first_line = 0
            while first_line < total_lines:
                available_lines = int((bottom - y - 6) // line_height)
                if available_lines < 1:
                    y = next_page()
                    continue
                count = min(available_lines, total_lines - first_line)
                painter.setPen(QColor("#24354a"))
                for line_number in range(first_line, first_line + count):
                    title_layout.lineAt(line_number).draw(painter, QPointF(3, y + 3 - first_line * line_height))
                y += count * line_height + 6
                first_line += count
                if first_line < total_lines:
                    y = next_page()
        if not report.rows:
            painter.setFont(row_font)
            painter.setPen(QColor("#526779"))
            painter.drawText(QRectF(3, y + 5, width - 6, 18),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), "No tasks in this project.")
        for index, row in enumerate(report.rows):
            font = QFont(row_font)
            font.setBold(row.summary)
            line_height = math.ceil(QFontMetricsF(font).height() + 1)
            indent = min(row.depth * 7, widths[1] * 0.3)
            layouts = []
            line_counts = []
            for column, value in enumerate(_row_values(row, show_time)):
                inset = indent if column == 1 else 0
                layout, count = _text_layout(value, font, widths[column] - 5 - inset, line_height)
                layouts.append(layout)
                line_counts.append(count)
            total_lines = max(line_counts)
            first_line = 0
            while first_line < total_lines:
                available_lines = int((bottom - y - 6) // line_height)
                if available_lines < 1:
                    y = next_page()
                    continue
                count = min(available_lines, total_lines - first_line)
                segment_height = count * line_height + 6
                if index % 2:
                    painter.fillRect(QRectF(0, y, width, segment_height), QColor("#f8fafc"))
                painter.setPen(QColor("#24354a"))
                for column, layout in enumerate(layouts):
                    inset = indent if column == 1 else 0
                    painter.save()
                    painter.setClipRect(QRectF(positions[column] + 2, y + 3, widths[column] - 4, count * line_height))
                    origin = QPointF(positions[column] + 2 + inset, y + 3 - first_line * line_height)
                    for line_number in range(first_line, min(first_line + count, layout.lineCount())):
                        layout.lineAt(line_number).draw(painter, origin)
                    painter.restore()
                if first_line == 0:
                    _draw_timeline(painter, report, row, QRectF(positions[-2], y, widths[-1], segment_height))
                painter.setPen(QPen(QColor("#dfe6ed"), 0.5))
                painter.drawLine(QPointF(0, y + segment_height), QPointF(width, y + segment_height))
                y += segment_height
                first_line += count
                if first_line < total_lines:
                    y = next_page()
    finally:
        ended = painter.end()
        del painter
        del writer
    if not ended:
        raise OSError("Could not finish the PDF")


def export_pdf(plan: Plan, path: str | Path, paper: str = "A4", show_time: bool = False) -> None:
    if paper not in PAPER_SIZES:
        raise ValueError("PDF page size must be A4 or A3")
    report = make_report(plan)
    write_atomically(path, lambda temporary: _render_pdf(report, temporary, paper, show_time))
