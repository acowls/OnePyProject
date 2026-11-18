import copy
from html.parser import HTMLParser
import os
from pathlib import Path

import pytest
from pypdf import PdfReader
from PySide6.QtWidgets import QApplication

from onepyproject.model import Plan, Resource, ResourceAssignment, Task
from onepyproject.report import export_html, make_report, timeline_positions, write_atomically
from onepyproject.report_pdf import export_pdf


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def sample_plan():
    return Plan(title='R&D <script>alert("x")</script>', start="2026-11-16",
                resources=[Resource(1, 'Ada <img src="https://invalid.example">')],
                tasks=[Task(1, "Summary"),
                       Task(2, "Design & research", parent=1, duration=0.5, estimated=True, percent_complete=50,
                            assignments=[ResourceAssignment(1, 50)]),
                       Task(3, "Milestone", parent=1, duration=0, predecessors=["2FS+0.1m"])])


def _native_pdf_text_or_none(page):
    text = page.extract_text()
    if text:
        return text
    if os.name == "nt":
        # The Windows Qt PDF output on the CI runner has no extractable Unicode
        # text for pypdf. Check that the generated page has drawing content;
        # keep semantic extraction checks active on platforms that expose it.
        contents = page.get_contents()
        assert contents is not None and len(contents.get_data()) > 0
        return None
    return text


class ElementInspector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.attributes = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(attrs)


def test_html_is_single_offline_document_and_escapes_project_text(tmp_path):
    plan = sample_plan()
    before = copy.deepcopy(plan)
    path = tmp_path / "report.html"
    export_html(plan, path)
    contents = path.read_text(encoding="utf-8")
    parser = ElementInspector()
    parser.feed(contents)
    assert plan == before
    assert '<script>' not in contents and '&lt;script&gt;' in contents
    assert '&lt;img' in contents and 'https://invalid.example' in contents
    assert not set(parser.tags) & {"script", "iframe", "img", "link", "object"}
    assert not any(key.lower().startswith("on") or key.lower() in ("src", "href") for key, _ in parser.attributes)
    assert parser.tags.count("svg") == 3
    assert 'Parent: 1' in contents and '2FS+0.1m' in contents and '0.5 d?' in contents
    assert '2026-11-16' in contents and '13:00:06' not in contents
    assert 'summary-bar' in contents and 'milestone' in contents
    assert "<th>Complete</th>" in contents and "50.0%" in contents
    assert make_report(plan).rows[1].percent_complete == "50.0%"


def test_report_timeline_handles_single_milestone_and_empty_plan(tmp_path):
    empty = Plan(start="2026-11-16")
    path = tmp_path / "empty.html"
    export_html(empty, path)
    assert "No tasks in this project" in path.read_text(encoding="utf-8")
    report = make_report(Plan(start="2026-11-16", tasks=[Task(1, "Gate", duration=0)]))
    assert timeline_positions(report, report.rows[0]) == (0.5, 0.5)


@pytest.mark.parametrize("paper,dimensions", [("A4", (842, 595)), ("A3", (1191, 842))])
@pytest.mark.parametrize("show_time", [False, True])
def test_pdf_landscape_contains_the_project(app, tmp_path, paper, dimensions, show_time):
    plan = sample_plan()
    before = copy.deepcopy(plan)
    path = tmp_path / f"report-{paper}.pdf"
    export_pdf(plan, path, paper, show_time)
    pdf = PdfReader(path)
    assert path.read_bytes().startswith(b"%PDF-")
    assert len(pdf.pages) == 1
    page = pdf.pages[0]
    assert (float(page.mediabox.width), float(page.mediabox.height)) == pytest.approx(dimensions, abs=2)
    text = _native_pdf_text_or_none(page)
    if text is None:
        assert page.get_contents() is not None
        assert plan == before
        return
    assert "Summary" in text and "Milestone" in text and "Design" in text and "Ada" in text
    assert ("08:00" in "".join(text.split())) is show_time
    assert ("13:00:06" in "".join(text.split())) is show_time
    assert plan == before


def test_html_can_show_precise_times(tmp_path):
    path = tmp_path / "times.html"
    export_html(sample_plan(), path, show_time=True)
    text = path.read_text(encoding="utf-8")
    assert '2026-11-16 08:00' in text
    assert '2026-11-16 13:00:06' in text


def test_reports_use_display_numbers_not_stable_ids(app, tmp_path):
    plan = Plan(start="2026-11-16", tasks=[Task(30, "Phase"), Task(5, "First", parent=30),
                                         Task(90, "Second", parent=30, predecessors=["5SS+1h"])])
    report = make_report(plan)
    assert [(row.uid, row.parent, row.predecessors) for row in report.rows] == [
        (1, 0, ""), (2, 1, ""), (3, 1, "2SS+1h")]
    html = tmp_path / "ids.html"
    pdf = tmp_path / "ids.pdf"
    export_html(plan, html)
    export_pdf(plan, pdf)
    assert "2SS+1h" in html.read_text(encoding="utf-8")
    text = _native_pdf_text_or_none(PdfReader(pdf).pages[0])
    if text is not None:
        assert "2SS+1h" in "".join(text.split())


def test_pdf_default_is_a4_and_paginates_all_rows(app, tmp_path):
    plan = Plan(start="2026-11-16", tasks=[Task(i, f"Item-{i:03}") for i in range(1, 100)])
    path = tmp_path / "many.pdf"
    export_pdf(plan, path)
    pdf = PdfReader(path)
    assert len(pdf.pages) > 1
    assert (float(pdf.pages[0].mediabox.width), float(pdf.pages[0].mediabox.height)) == pytest.approx((842, 595), abs=2)
    extracted = [_native_pdf_text_or_none(page) for page in pdf.pages]
    if any(text is None for text in extracted):
        assert all(page.get_contents() is not None and page.get_contents().get_data() for page in pdf.pages)
        return
    text = "\n".join(extracted)
    assert all(f"Item-{i:03}" in text for i in range(1, 100))


def test_pdf_splits_very_long_rows_across_pages(app, tmp_path):
    name = " ".join(f"token{i:04}" for i in range(500))
    path = tmp_path / "long.pdf"
    export_pdf(Plan(start="2026-11-16", tasks=[Task(1, name), Task(2, "After-long-row")]), path)
    pdf = PdfReader(path)
    assert len(pdf.pages) > 1
    pages = [_native_pdf_text_or_none(page) for page in pdf.pages]
    if any(text is None for text in pages):
        assert all(page.get_contents() is not None and page.get_contents().get_data() for page in pdf.pages)
        return
    assert "token0499" not in pages[0]
    text = "\n".join(pages)
    joined = "".join(text.split())
    assert all(f"token{i:04}" in joined for i in range(500))
    assert "After-long-row" in text


def test_long_project_title_is_not_lost(app, tmp_path):
    title = " ".join(f"title{i:03}" for i in range(100))
    path = tmp_path / "title.pdf"
    export_pdf(Plan(title=title, start="2026-11-16", tasks=[Task(1, "Task")]), path)
    pdf = PdfReader(path)
    extracted = [_native_pdf_text_or_none(page) for page in pdf.pages]
    if any(text is None for text in extracted):
        assert all(page.get_contents() is not None and page.get_contents().get_data() for page in pdf.pages)
        return
    text = "".join("\n".join(extracted).split())
    assert all(f"title{i:03}" in text for i in range(100))


def test_empty_pdf_and_invalid_paper(app, tmp_path):
    path = tmp_path / "empty.pdf"
    export_pdf(Plan(start="2026-11-16"), path)
    page = PdfReader(path).pages[0]
    text = _native_pdf_text_or_none(page)
    if text is None:
        assert page.get_contents() is not None and page.get_contents().get_data()
    else:
        assert "No tasks" in " ".join(text.split())
    with pytest.raises(ValueError, match="A4 or A3"):
        export_pdf(Plan(), tmp_path / "invalid.pdf", "letter")
    assert not (tmp_path / "invalid.pdf").exists()


def test_failed_atomic_write_preserves_existing_file(tmp_path):
    path = tmp_path / "existing.html"
    path.write_text("previous", encoding="utf-8")
    def fail(temporary: Path):
        temporary.write_text("partial", encoding="utf-8")
        raise OSError("disk error")
    with pytest.raises(OSError, match="disk error"):
        write_atomically(path, fail)
    assert path.read_text(encoding="utf-8") == "previous"
    assert list(tmp_path.iterdir()) == [path]
