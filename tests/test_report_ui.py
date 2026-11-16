import copy
import os
from pathlib import Path

import pytest
from pypdf import PdfReader
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QFileDialog, QMessageBox

from onepyproject.app import ExportDialog, MainWindow
from onepyproject.model import Plan, Task
from onepyproject.mpp import EXPORT_WARNING


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path):
    preferences = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(preferences)
    window.plan = Plan(start="2026-11-16", tasks=[Task(1, "Unsaved task")])
    window.dirty = True
    window.refresh()
    yield window
    window.dirty = False
    window.close()


def choose_export(dialog, path, kind, *, show_time=False, paper="A4", template=None):
    dialog.set_format(kind)
    dialog.selectFile(str(path))
    dialog.show_time.setChecked(show_time)
    dialog.paper.setCurrentIndex(dialog.paper.findData(paper))
    if template is not None:
        dialog.template_edit.setText(str(template))
    dialog.accept()
    return dialog.result()


def test_export_dialog_shows_destination_and_options_on_one_page(app, tmp_path):
    dialog = ExportDialog(initial_path=tmp_path / "example.mpp")
    dialog.show()
    app.processEvents()
    assert dialog.testOption(QFileDialog.Option.DontUseNativeDialog)
    assert dialog.nameFilters() == list(ExportDialog.FILTERS.values())
    assert dialog.selected_format() == "mpp"
    assert dialog.filename_edit.isVisible() and dialog.filename_edit.text() == "example.mpp"
    assert Path(dialog.directory().absolutePath()).resolve() == tmp_path.resolve()
    assert dialog.template_row.isVisible() and not dialog.template_edit.text()
    assert dialog.mpp_warning.isVisible() and dialog.mpp_warning.text() == EXPORT_WARNING
    assert EXPORT_WARNING == (
        "An empty MPP template is required to save this project as an MPP. "
        "Some metadata may be retained from the template in the saved file.\n\n"
        "MPP support is experimental. Save OnePyProject (.pxplan) to retain your work."
    )
    assert not dialog.show_time.isVisible() and not dialog.paper.isVisible()
    dialog.set_format("html")
    assert dialog.filename_edit.text() == "example.html"
    assert not dialog.mpp_warning.isVisible() and dialog.show_time.isVisible()
    assert not dialog.template_row.isVisible() and not dialog.show_time.isChecked() and not dialog.paper.isVisible()
    dialog.set_format("pdf")
    assert dialog.filename_edit.text() == "example.pdf"
    assert not dialog.mpp_warning.isVisible() and dialog.show_time.isVisible() and dialog.paper.isVisible()
    assert [dialog.paper.itemData(i) for i in range(dialog.paper.count())] == ["A4", "A3"]
    assert dialog.paper.currentData() == "A4" and not dialog.show_time.isChecked()
    dialog.close()


def test_export_button_opens_dialog_and_html_is_connected(window, tmp_path, monkeypatch):
    path = tmp_path / "menu.html"
    def choose_html(dialog):
        return choose_export(dialog, path, "html")
    monkeypatch.setattr(ExportDialog, "exec", choose_html)
    def unexpected(*args, **kwargs):
        raise AssertionError("A second file dialog should not open")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", unexpected)
    action = next(action for action in window.file_toolbar.actions() if action.text() == "Export")
    action.trigger()
    assert "Unsaved task" in path.read_text(encoding="utf-8")


def test_saved_project_suggests_filename_and_folder(window, tmp_path, monkeypatch):
    window.plan_path = tmp_path / "Project name.pxplan"
    seen = []
    def inspect(dialog):
        seen.append((dialog.filename_edit.text(), Path(dialog.directory().absolutePath()).resolve()))
        dialog.set_format("pdf")
        seen.append((dialog.filename_edit.text(), Path(dialog.directory().absolutePath()).resolve()))
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(ExportDialog, "exec", inspect)
    window.export_project()
    assert seen == [("Project name.mpp", tmp_path.resolve()), ("Project name.pdf", tmp_path.resolve())]


def test_export_html_includes_unsaved_edits_without_saving(window, tmp_path, monkeypatch):
    path = tmp_path / "snapshot"
    before = copy.deepcopy(window.plan)
    monkeypatch.setattr(ExportDialog, "exec", lambda dialog: choose_export(dialog, path, "html"))
    window.export_project()
    assert "Unsaved task" in (tmp_path / "snapshot.html").read_text(encoding="utf-8")
    assert window.dirty and window.plan == before and window.plan_path is None


@pytest.mark.parametrize("paper,dimensions", [("A4", (842, 595)), ("A3", (1191, 842))])
@pytest.mark.parametrize("show_time", [False, True])
def test_export_pdf_passes_page_size_and_time_choice(window, tmp_path, monkeypatch, paper, dimensions, show_time):
    def choose(dialog):
        return choose_export(dialog, tmp_path / "snapshot", "pdf", paper=paper, show_time=show_time)
    monkeypatch.setattr(ExportDialog, "exec", choose)
    window.export_project()
    pdf = PdfReader(tmp_path / "snapshot.pdf")
    page = pdf.pages[0]
    assert (float(page.mediabox.width), float(page.mediabox.height)) == pytest.approx(dimensions, abs=2)
    text = page.extract_text()
    if text:
        assert ("08:00" in "".join(text.split())) is show_time
    elif os.name == "nt":
        assert page.get_contents() is not None and page.get_contents().get_data()
    else:
        pytest.fail("PDF has no extractable text")
    assert window.dirty and window.plan_path is None


def test_cancelling_export_does_not_open_file_dialog(window, monkeypatch):
    monkeypatch.setattr(ExportDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    def unexpected(*args, **kwargs):
        raise AssertionError("File dialog should not be opened")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", unexpected)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", unexpected)
    window.export_project()


@pytest.mark.parametrize("show_time", [False, True])
def test_export_html_passes_time_choice(window, tmp_path, monkeypatch, show_time):
    path = tmp_path / "snapshot.html"
    def choose(dialog):
        return choose_export(dialog, path, "html", show_time=show_time)
    monkeypatch.setattr(ExportDialog, "exec", choose)
    window.export_project()
    assert ("08:00" in path.read_text(encoding="utf-8")) is show_time


def test_mpp_selection_uses_the_template_and_destination_from_dialog(window, tmp_path, monkeypatch):
    template = tmp_path / "template.mpp"
    template.write_bytes(b"template")
    output = tmp_path / "output.mpp"
    calls = []
    monkeypatch.setattr(ExportDialog, "exec", lambda dialog: choose_export(dialog, output, "mpp", template=template))
    monkeypatch.setattr("onepyproject.app.export_mpp", lambda plan, source, target, protected: calls.append((source, target)) or [])
    monkeypatch.setattr(QMessageBox, "information", lambda *args: QMessageBox.StandardButton.Ok)
    window.export_project()
    assert calls == [(template, output)]


def test_save_as_mpp_can_switch_to_html_without_writing_the_mpp_path(window, tmp_path, monkeypatch):
    mpp_path = tmp_path / "selected.mpp"
    html_path = tmp_path / "report.html"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(mpp_path), "Microsoft Project (*.mpp)"))
    def choose_html(dialog):
        assert dialog.selected_format() == "mpp" and EXPORT_WARNING in dialog.mpp_warning.text()
        return choose_export(dialog, html_path, "html")
    monkeypatch.setattr(ExportDialog, "exec", choose_html)
    window.save_as()
    assert html_path.exists() and not mpp_path.exists()


def test_existing_report_is_not_replaced_without_confirmation(window, tmp_path, monkeypatch):
    path = tmp_path / "existing.html"
    path.write_text("old contents", encoding="utf-8")
    monkeypatch.setattr(ExportDialog, "exec", lambda dialog: choose_export(dialog, path.with_suffix(""), "html"))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    window.export_project()
    assert path.read_text(encoding="utf-8") == "old contents"


def test_report_cannot_replace_a_link_to_current_plan(window, tmp_path, monkeypatch):
    source = tmp_path / "project.pxplan"
    source.write_text("working plan", encoding="utf-8")
    link = tmp_path / "report.html"
    try:
        link.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks are unavailable")
    window.plan_path = source
    errors = []
    monkeypatch.setattr(ExportDialog, "exec", lambda dialog: choose_export(dialog, link, "html"))
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    window.export_project()
    assert errors and "Cannot overwrite" in errors[0]
    assert source.read_text(encoding="utf-8") == "working plan" and link.is_symlink()


def test_mpp_requires_explicit_template_and_never_overwrites(app, tmp_path, monkeypatch):
    output = tmp_path / "existing.mpp"
    output.write_bytes(b"existing")
    template = tmp_path / "template.mpp"
    template.write_bytes(b"template")
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[-1]))
    dialog = ExportDialog(initial_format="mpp", initial_path=output)
    dialog.accept()
    assert dialog.output_path is None and warnings and "template" in warnings[-1].lower()
    dialog.template_edit.setText(str(template))
    dialog.accept()
    assert dialog.output_path is None and "does not overwrite" in warnings[-1]
    assert output.read_bytes() == b"existing" and template.read_bytes() == b"template"


def test_mpp_cannot_overwrite_its_template_via_a_hardlink(app, tmp_path, monkeypatch):
    template = tmp_path / "template.mpp"
    template.write_bytes(b"template")
    target = tmp_path / "hardlink.mpp"
    try:
        target.hardlink_to(template)
    except OSError:
        pytest.skip("Hard links are unavailable")
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[-1]))
    dialog = ExportDialog(initial_format="mpp", initial_path=target)
    dialog.template_edit.setText(str(template))
    dialog.accept()
    assert dialog.output_path is None and "Cannot overwrite" in warnings[-1]
    assert template.read_bytes() == b"template"


@pytest.mark.parametrize("filename,expected", [
    ("report", "report.pdf"), ("report.v1", "report.v1.pdf"),
    ("report.mpp", "report.pdf"), ("report.PDF", "report.PDF"),
    ("project plan draft", "project plan draft.pdf"),
])
def test_destination_extension_matches_selected_format(app, tmp_path, filename, expected):
    dialog = ExportDialog(initial_format="pdf", initial_path=tmp_path / filename)
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.output_path == tmp_path / expected


def test_destination_requires_a_filename(app, tmp_path):
    dialog = ExportDialog(initial_format="html")
    dialog.setDirectory(str(tmp_path))
    dialog.filename_edit.clear()
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted and dialog.output_path is None


@pytest.mark.parametrize("method", ["button", "enter"])
def test_export_dialog_accepts_from_button_or_keyboard(app, tmp_path, method):
    dialog = ExportDialog(initial_format="pdf", initial_path=tmp_path / "keyboard")
    dialog.show()
    app.processEvents()
    if method == "button":
        box = dialog.findChild(QDialogButtonBox, "buttonBox")
        box.button(QDialogButtonBox.StandardButton.Save).click()
    else:
        QTest.keyClick(dialog.filename_edit, Qt.Key.Key_Return)
    app.processEvents()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.output_path == tmp_path / "keyboard.pdf"


def test_selected_folder_and_filename_are_used_together(app, tmp_path):
    folder = tmp_path / "chosen folder"
    folder.mkdir()
    dialog = ExportDialog(initial_format="html")
    dialog.setDirectory(str(folder))
    dialog.filename_edit.setText("plan")
    dialog.accept()
    assert dialog.output_path == folder / "plan.html"


def test_entering_a_folder_navigates_without_closing_dialog(app, tmp_path):
    folder = tmp_path / "chosen"
    folder.mkdir()
    dialog = ExportDialog(initial_format="pdf")
    dialog.setDirectory(str(tmp_path))
    dialog.filename_edit.setText(folder.name)
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted and dialog.output_path is None
    assert Path(dialog.directory().absolutePath()).resolve() == folder.resolve()
    assert not dialog.filename_edit.text()
    dialog.filename_edit.setText("report")
    dialog.accept()
    assert dialog.output_path == folder / "report.pdf"


def test_mpp_relative_template_path_uses_selected_folder(app, tmp_path):
    template = tmp_path / "template.mpp"
    template.write_bytes(b"template")
    dialog = ExportDialog(initial_format="mpp")
    dialog.setDirectory(str(tmp_path))
    dialog.filename_edit.setText("output")
    dialog.template_edit.setText("template.mpp")
    dialog.accept()
    assert dialog.output_path == tmp_path / "output.mpp" and dialog.template_path == template


def test_missing_destination_directory_keeps_dialog_open(app, tmp_path, monkeypatch):
    dialog = ExportDialog(initial_format="pdf")
    dialog.filename_edit.setText(str(tmp_path / "missing" / "output"))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[-1]))
    dialog.accept()
    assert dialog.output_path is None and dialog.result() != QDialog.DialogCode.Accepted
    assert warnings and "destination folder" in warnings[-1]


def test_existing_report_is_replaced_only_after_confirmation(window, tmp_path, monkeypatch):
    path = tmp_path / "existing.html"
    path.write_text("old contents", encoding="utf-8")
    monkeypatch.setattr(ExportDialog, "exec", lambda dialog: choose_export(dialog, path, "html"))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window.export_project()
    assert "Unsaved task" in path.read_text(encoding="utf-8")


def test_template_browser_sets_field_without_choosing_destination(app, tmp_path, monkeypatch):
    template = tmp_path / "template.mpp"
    template.write_bytes(b"template")
    dialog = ExportDialog(initial_path=tmp_path / "output.mpp")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(template), ""))
    dialog.browse_template()
    assert dialog.template_edit.text() == str(template)
    assert dialog.filename_edit.text() == "output.mpp" and dialog.output_path is None
