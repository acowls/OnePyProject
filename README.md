# OnePyProject

An experimental offline project planner for Windows 11 and macOS, built with Python, Qt and pymppwriter. The current version is **0.1.0a1** (the first 0.1.0 alpha). The editable OnePyProject plan (`.pxplan`) is separate from `MPP` interchange that can also be read and written.

## Run from source

Python 3.10 or newer is required. In the project directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m onepyproject.app
```

On Windows PowerShell, create the virtual environment with `py -m venv .venv`, then use `.venv\Scripts\Activate.ps1` and run the remaining Python commands. Installers are not yet provided.

The **File** tab contains New, Open, Save, Save As, Import, and Export; the **Project** tab contains task controls, Resources, Assign, and Settings. Toolbar controls show icons only by default; hover for their names or enable **Show toolbar icon names** in Settings. Click **+ Task** to add tasks. Double-click an editable cell (or select it and press F2) to edit it directly; **Edit** opens the full task dialog. Start edits specify an earliest start, so dependencies may move the task later. Summary values, finish dates, IDs, and calculated work or duration are read-only. Use **Indent / Outdent** to create summaries. Select multiple tasks using Shift or Command/Ctrl and choose **Link** to create finish-to-start links in row order, or **Unlink** to remove links between the selected tasks. Displayed task IDs follow outline order, including collapsed children. Inserting, deleting or moving tasks updates the displayed IDs and predecessor numbers while keeping links to the same tasks. **Undo / Redo** support up to 100 changes and the usual keyboard shortcuts. **Settings** controls the project start and holidays. **Open** accepts `.pxplan` and MPP14 `.mpp` files; **Import** is an alternative way to open an MPP. MPP opening imports a potentially incomplete copy and recalculates it, leaving the original unchanged. **Save** saves the working `.pxplan`; **Save As** offers `.pxplan` or `.mpp`. **Export** opens a dialog for MPP, HTML and PDF copies. MPP export shows its warning in the dialog, asks for a template and will not overwrite any existing MPP. Export does not replace saving the working plan.

Choose the destination folder, file name and export type together in the Export window. For MPP, select a user-supplied template in that window; existing MPP files are never overwritten. For HTML or PDF, confirm before replacing an existing file.

The HTML and PDF exports are read-only snapshots of the current plan, including unsaved edits. They contain the task table and a Gantt chart scaled across the full project; predecessors are listed rather than drawn as arrows. Dates omit times by default; select **Show times (HH:MM)** in the export dialog to include them. HTML is one self-contained file with no scripts or external resources and opens offline in a browser. PDF defaults to A4 landscape, with A3 landscape available, and spans pages for long task lists. Both include project data, so share them only with intended recipients. Email providers or organisational policies may still warn about or block attachments. Continue to save a `.pxplan` for editing.

Use **Resources** to add or rename people and teams, set their maximum allocation, and see overallocation warnings. Select a task and choose **Assign** to assign several resources at individual percentages. Alternatively, enter names in the Resource cell, such as `Ada[50%], Ben`; existing resources are suggested as you type, and new names are added to the resource sheet. Select a suggestion with Enter, Tab, or a click; press Enter again to save the cell. Escape dismisses suggestions without choosing one. Resource names containing commas can be quoted, for example `"Ada, Senior[50%]", Ben`. If a name is ambiguous, use **Assign**. Removing a resource requires removing its assignments first. Overallocation warnings do not move tasks. Older `.pxplan` files are migrated when opened; newly saved plans use version 2 and may not open in earlier versions of the application.

Double-click the empty row or empty area beneath tasks, or select it and press **Enter**, to add one. Press **Insert** or **Shift+Enter** on an existing task to add a task above it at the same outline level. With a task selected, **Tab** indents it, **Shift+Tab** outdents it, and **Delete/Backspace** deletes it after confirmation; text-editing keys keep their usual behavior while a cell editor is open. Typing a digit on an editable task starts entering its duration in days; this shortcut can be turned off in **Settings** and is remembered for the application. Type a question mark after a duration, such as `2d?`, to mark it estimated; remove the question mark to mark it confirmed. The task dialog also has an estimate checkbox. A summary shows `?` when marked estimated itself or when a descendant is estimated. Milestones and durations of at least one day display dates without times; hover over Start or Finish to see the exact time. Use **− / +** in the bottom bar to change the Gantt timescale up to 100% without changing the schedule. Choose **Comfortable** (default), **Tight**, or **Dense** in the bottom bar to adjust task and resource row spacing. Previously saved Tight and Dense preferences retain their old spacing under the new Comfortable and Tight names; the old Comfortable setting now uses the new Comfortable spacing.

Choose **System**, **11 Nov HH:MM**, or **YYYY-MM-DD HH:MM** in Settings to change the date display. System uses the operating system's locale. The chosen display format is remembered for the application; date entry remains `YYYY-MM-DD`. Duration, Start, Finish and Work start at content-sized widths and can be resized.

Application preferences, including task-column widths, toolbar names, row density, date format, keyboard behavior and zoom, are stored per user with Qt's native settings. The exact location is shown in **Settings**. On macOS it is typically `~/Library/Preferences/com.onepyproject.OnePyProject.plist`; on Windows it is typically `HKEY_CURRENT_USER\Software\OnePyProject\OnePyProject` in the registry. Existing ProjectX preferences are copied once without overwriting new settings or deleting the originals. Project-specific details remain in the `.pxplan` file. The app warns if preferences cannot be saved.

## Scope and limitations

- The scheduler supports FS, SS, FF and SF links, working-time and elapsed lead/lag, multiple resources per task, fixed-duration or fixed-work tasks, zero-duration milestones and summary rollups. Enter displayed task IDs in the Predecessors cell, for example `5`, `5SS+3d`, `5FF-1d`, `5SF+2d`, `5FS+50%`, or `5FS+2ed`; separate multiple links with commas. FS is the default, and a lag without a unit is treated as working days (`5SF+2` becomes `5SF+2d`). Percentage lag is based on predecessor working duration. Working units are `m`, `h`, `d`, `w`, `mo` (1 day = 8 hours, 1 week = 5 days, 1 month = 20 days); elapsed units are `em`, `eh`, `ed`, `ew`, `emo` (24-hour days, 7-day weeks, 30-day months). Elapsed calculations use local wall-clock time without daylight-saving adjustments. Summary-task dependencies are not supported.
- The calendar is Monday–Friday, 08:00–12:00 and 13:00–17:00, with optional whole-day holidays. No resource levelling or resource-specific calendars are performed. Imported schedules are recalculated and can change. The scheduler has not been verified against Microsoft Project for all dependency combinations, especially finish constraints at non-working boundaries.
- Work is hours, and each allocation is a percentage. Fixed-work duration is work divided by the sum of assigned allocations; at least one resource is required. Fixed-duration tasks calculate work from duration and the sum of allocations. All resources share the project calendar. Individual calendars, costs, time-varying allocation and automatic levelling are not supported. Overallocation warnings only consider overlapping working time and do not change the schedule. Summary-task assignments may be retained on import but are not scheduled and must be removed before MPP export. This is a simplified model and does not claim full Microsoft Project scheduling equivalence.
- pymppwriter is alpha software. MPP import and export are **lossy** and have not yet been verified in Microsoft Project. Resource names, maximum units and multiple positive-percentage assignments are imported and exported; fixed-units tasks are approximated as fixed-duration, and zero-percentage assignments are reported and omitted. All four dependency types and working lags can be written; working lags are exported as days. MPP export refuses nonzero elapsed or percentage lags because pymppwriter cannot preserve their units. Import omits nonzero lags whose units the reader cannot safely identify and reports them. The reader can also omit unscheduled tasks and does not fully read calendars or task types. Resource calendars, rates, costs, progress, timephased data, summary-task dependencies, constraints, baselines, custom data and other Project features are not preserved in the OnePyProject model. Import reports some known omissions, not necessarily all losses. Keep the source MPP unchanged and verify exported files in Microsoft Project before relying on them.
- MPP export requires a user-supplied `template.mpp`, which is not included in the repository. OnePyProject removes its resource records before generating a new MPP, but pymppwriter retains other template streams, so exports may still include hidden project data, names, metadata, or other private information. Use a suitably licensed, minimal template and audit exports before distribution. Do not assume that deleting visible tasks removes hidden data.
- The Gantt view supports at most 5,000 days of timeline from its start. Time and dates are local wall-clock values, with no timezone conversions.


## Tests

```bash
python -m pytest
```

The optional integration test uses `template.mpp` if present and writes only to a temporary directory. It checks pymppwriter read-back, **not** Microsoft Project compatibility. Test representative exports in a licensed copy of Microsoft Project, both before and after recalculation.

## Packaging

The manual **Private desktop builds** GitHub Actions workflow builds macOS Apple Silicon, macOS Intel and Windows x64 separately. To build the current alpha:

1. Commit and push the changes containing the desired version to the branch you want to build.
2. In the repository on GitHub, open **Actions → Private desktop builds → Run workflow**, select that branch and click **Run workflow**.
3. Open the completed run and download the matching artifact. Artifact names and `BUILD_INFO.json` include the version; artifacts expire after seven days.

The version is defined in `onepyproject/__init__.py` using Python's pre-release format (`0.1.0a1`, `0.1.0a2`, and so on). This workflow builds whatever version is on the selected branch; it does not create a GitHub release. Private-repository runner and artifact usage may incur charges. The macOS builds require macOS 14 or newer. The Windows build runs its automated checks on a Windows Server runner; it still needs testing on Windows 11.

Each artifact contains a ZIP: extract it fully, then open `OnePyProject.app` on macOS or `OnePyProject/OnePyProject.exe` on Windows. The app is not signed with a developer identity or notarized, so operating-system security warnings are expected. These are **private test builds, not public releases**. No installer is provided.

To build locally in a clean Python 3.12 environment on the target OS and architecture, install `python -m pip install -e '.[dev]' -r packaging/requirements-build.txt`, run `python -m PyInstaller --noconfirm packaging/OnePyProject.spec`, then run `python packaging/package_artifact.py TARGET` with `macos-arm64`, `macos-x64` or `windows-x64`. The script checks for personal plans and templates, archives the app and runs a noninteractive smoke test on the extracted build. The pinned main build dependencies do not constitute a complete reproducible lockfile.

The CI smoke test checks plan saving, the window, SVG icons, HTML and A4/A3 PDF export, but cannot test MPP import/export without a template. To include that local check, set `ONEPYPROJECT_SMOKE_MPP_TEMPLATE` to your external template's absolute path before running `package_artifact.py`; the test writes a disposable MPP in a temporary directory and checks that the template remains unchanged.

The packaging uses separate Qt libraries and includes SVG assets and plugins, but this alone does **not** establish LGPL compliance. Before distribution, review all bundled third-party licences, required notices and corresponding source, library replacement, signing and notarization. See `packaging/THIRD_PARTY_NOTICES.md`; it is not a complete compliance inventory. Keep `template.mpp` external and select it at export time.

## License

OnePyProject's own code is licensed under the [MIT licence](LICENSE). Its dependencies have separate licences: the free Qt/PySide6 distribution has LGPL/GPL obligations. Check all licences and how to comply before distributing a packaged application; signing and notarization can have separate costs.

Toolbar artwork uses [Icons8 Flat Color Icons](https://github.com/icons8/flat-color-icons), under the MIT licence option. The included attribution and licence are in `onepyproject/assets/flat_color_icons/LICENSE.txt`.
