# OnePyProject design decisions

Status: prototype, reviewed 13 November 2026. This document records the agreed direction and the current implementation; it does not claim full Microsoft Project compatibility.

## 1. Goal and boundaries

- Build a free, offline desktop alternative to Microsoft Project for Windows 11 and macOS.
- Use the Microsoft Project Gantt view as the starting point: an editable task grid on the left, a synchronized timeline on the right, hierarchical summary tasks, milestones and dependency lines.
- Support task scheduling by duration or effort and exchange plans in Microsoft Project's binary **MPP14** (`.mpp`) format, used by Project 2010 and later.
- Deliver the basics first. Full scheduling and lossless MPP round trips are goals to investigate, not established capabilities.

## 2. Technology

- **Python 3.10+** for the application, scheduling model and interchange adapter.
- **PySide6 / Qt** for a single cross-platform desktop interface, using a Qt tree grid and custom Gantt drawing. No server or Java runtime is needed by the shipped application.
- **pymppwriter 0.4.3**, pinned, for experimental MPP14 reading and writing. It is alpha software and uses a user-supplied MPP template.
- **pytest** for automated tests. **PyInstaller** is the planned packaging tool; builds must be produced and tested separately on Windows and macOS. Installers, signing and notarization are not yet validated.
- **Icons8 Flat Color Icons**, using its MIT licence option. SVGs are bundled locally and passed to Qt as scalable icons for high-DPI rendering. Licence and attribution are in `onepyproject/assets/flat_color_icons/LICENSE.txt`.

The selected libraries are free to use, subject to their licence obligations. In particular, PySide6/Qt's open-source licences must be reviewed before distributing a packaged app. Platform signing or notarization may have separate costs; no decision about the application's own distribution licence has been made.

Alternatives considered: Aspose.Tasks can write MPP but was rejected because it requires a commercial licence. MPXJ is a free MPP reader but cannot write MPP. Microsoft Project XML is a useful interchange fallback, but it is not the binary MPP file the user requested. The Python/pymppwriter stack was selected with the understanding that its alpha status and template-based output require extra verification.

## 3. Application and file architecture

- The application works locally and offline. `onepyproject/model.py` contains the project model and scheduler; `onepyproject/dependencies.py` handles dependency expressions; `onepyproject/resources.py` handles assignments and overallocation; `onepyproject/mpp.py` and `onepyproject/mpp_template.py` handle MPP interchange; `onepyproject/app.py` and `onepyproject/resource_ui.py` implement the interface.
- The editable source of truth is a JSON-based **`.pxplan`** file. Current saves use version 2; version 1 files are migrated on load. Version 2 files may not open in older versions of the application. Saves use a temporary file followed by replacement. The `.pxplan` extension and historical `projectx-plan` format identifier are deliberately retained so existing files remain compatible after the rename.
- **Open** accepts `.pxplan` and MPP14. Opening MPP imports an experimental, potentially incomplete copy and recalculates it. The original MPP is not modified. The user is warned before import.
- **Save** saves the `.pxplan` working file. **Save As** can write a `.pxplan` or export an MPP; **Export** also creates an MPP copy. MPP export does not mark an unsaved working plan as saved. Export refuses to overwrite an existing MPP, including the original and template, and publishes a new file from a temporary file.
- MPP14 is the binary Project format, not Project XML. **MSPDI XML is not currently imported or converted by OnePyProject**; pymppwriter does not accept it directly. An adapter could be built later if needed. MPXJ is useful as an independent reader/test oracle but does not write MPP and is not a runtime dependency.

### Template and MPP safety

- Export requires a suitable MPP14 template. The original local `template.mpp` is a populated software-development plan, not a blank template, and is intentionally excluded from version control.
- OnePyProject removes template resource records and resource-specific calendar records before building an export, using an adapter tied to pymppwriter 0.4.3's internals. This prevents those visible resources from being cloned into the exported resource list; it does **not** prove all hidden template data is removed.
- Other template streams, views, metadata or private data may remain. Use a suitably licensed minimal template and audit files before sharing. Do not overwrite or redistribute a template without checking rights.
- MPP import/export remains lossy and experimental. Keep originals and verify generated files in Microsoft Project. A successfully opened file or a pymppwriter/MPXJ read-back does not prove Project will preserve or recalculate it identically.

## 4. Interface decisions

- Controls are grouped into **File** (New, Open, Save, Save As, Import, Export) and **Project** (Settings, Resources, Assign and task controls) tabs. Project is selected initially. Toolbar buttons show icons without text by default; Settings can enable labels, and names remain available as tooltips and accessible labels.
- The leftmost column shows the stable task ID, followed by task name, duration, start, finish, predecessors, resources, work and mode. IDs used in predecessor expressions are OnePyProject task IDs, not changing row positions.
- A zero-duration task is shown as a milestone diamond. Summary bars, nested tasks, expand/collapse and dependency lines appear in the Gantt chart. The view can display at most 5,000 days from its timeline origin.
- Zoom **− / +** and its percentage appear in the bottom bar. Zoom changes the Gantt scale, not the schedule, and is capped at 100%. The percentage box is sized for `100%` and does not change width as zoom changes. Controls have explicit contrasting colours and accessible names.
- The bottom bar also offers **Comfortable**, **Tight** (default), and **Dense** row spacing. An existing saved choice is preserved. The choice applies to the task grid and resource tables, keeps the Gantt aligned, and is remembered as an application preference without modifying the project.
- Dates use the application preference **System** (default), **11Nov HH:MM**, or **YYYY-MM-DD HH:MM**. Milestones and tasks of one day or more show dates without times; shorter tasks show times. Tooltips show the full time. Date entry remains `YYYY-MM-DD`. Duration, Start, Finish and Work columns are initially sized for `Duration`, `2020-08-08`, `2020-08-08` and `888 h` respectively, and can be resized. Expand/collapse arrows are black for contrast.
- Keyboard, toolbar-label, zoom, density and date-display preferences are stored per user using Qt's native settings, not in the project file. The exact location is shown in Settings: typically `~/Library/Preferences/com.onepyproject.OnePyProject.plist` on macOS or `HKEY_CURRENT_USER\Software\OnePyProject\OnePyProject` on Windows. Existing ProjectX settings are copied once without overwriting newer values or deleting the originals. Failed preference writes are reported. Typing a digit on a selected editable task starts editing its duration in days; this shortcut defaults on and can be disabled in Settings. It does not replace text being edited, or edit a calculated summary/fixed-work duration.

### Editing

- Double-click an editable cell or press F2 to edit. **Edit** opens task details. IDs, finish dates, summary calculations and the calculated half of fixed-work/fixed-duration scheduling are read-only. Editing Start specifies an earliest allowed start; a dependency can move it later.
- Double-click an empty row or select it and press Enter to add a task. Insert or Shift+Enter on a task adds one above it at the same outline level; a canceled or empty entry makes no change.
- Tab indents; Shift+Tab outdents. Delete or Backspace removes the selected task after confirmation, including descendants and their links. Normal text-editing keys continue to work in an open editor.
- Select multiple tasks and choose Link for finish-to-start links in row order, or Unlink to remove links between those selected tasks.
- Undo/redo stores up to 100 in-memory changes, with platform-standard keyboard shortcuts. Opening or creating another project resets that history. The saved `.pxplan` determines the clean/dirty state.
- A `?` suffix marks an estimated duration, for example `2d?`; removing it marks the duration confirmed. Task details also expose the flag. A summary displays `?` when marked estimated itself or when a descendant is estimated. Estimates are stored in `.pxplan` and mapped to MPP where supported.

## 5. Scheduling model

### Calendar and task dates

- The current project calendar is Monday–Friday, 08:00–12:00 and 13:00–17:00, with configurable whole-day holidays. A working day is 8 hours, week 5 days and month 20 days.
- Schedules use local wall-clock values without timezone or daylight-saving conversions. Elapsed days, weeks and months are 24 hours, 7 days and 30 days respectively.
- Leaf tasks are recalculated from project start, any entered earliest-start date, predecessors and the supported resource model. Summary start/finish roll up from children, summary work is summed, and summary duration is working time across the rolled-up span.
- There is no automatic resource levelling, configurable working-week editor, individual task/resource calendar, time-varying allocation or resource-cost scheduling. Overallocations warn but do not move tasks.

### Dependencies

- Support **FS, SS, FF and SF**. FS is the default for a bare predecessor ID. Multiple predecessors are supported; cycles, unknown IDs and duplicate predecessors are rejected.
- Accept positive lag and negative lead with working `m`, `h`, `d`, `w`, `mo`; elapsed `em`, `eh`, `ed`, `ew`, `emo`; or `%` of predecessor working duration. Missing lag units default to working days, so `2SF+5` becomes `2SF+5d`.
- Dependencies involving summary tasks and external projects are not supported. FF/SF and working-time-boundary behavior are implemented but still need direct comparison against Microsoft Project.
- MPP export writes supported working-time lags as days. It refuses nonzero elapsed or percentage lags rather than silently change their meaning. Import reports and omits nonzero lag units that it cannot safely interpret.

### Resources and effort

- The chosen first resource scope is a shared **Resources** sheet and **Assign** dialog: named people or teams, stable IDs, maximum units, multiple assignments per task and per-assignment percentages.
- The Resource cell also accepts names such as `Ada[50%], Ben`; unknown names are added to the sheet. Inline editing suggests existing resource names case-insensitively, completing the current name without replacing other assignments or their percentages. Names containing commas can be quoted. A referenced resource must be unassigned before deletion. Name ambiguities are resolved through the Assign dialog.
- **Fixed work:** duration = work hours / (8 hours × sum of assignment units). At least one positively allocated resource is required. **Fixed duration:** work = duration in days × 8 hours × sum of assignment units. Percentages are converted to fractions for these calculations.
- A resource's maximum units are used for overlap/overallocation warnings, not to cap work or automatically delay tasks. All assigned resources currently share the project calendar and have constant allocation over a task.
- Resource assignments retained on imported summary tasks are not scheduled; MPP export refuses them until removed. OnePyProject also prevents indenting under an assigned task when that would create a new summary with an unscheduled assignment.
- These rules are a simplified subset. Microsoft Project's fixed-units type, separate effort-driven behavior, progress/actual work, work contours, resource calendars and costs are not fully modelled.

## 6. MPP mapping and known fidelity risks

- The supported subset maps task hierarchy, dates, durations, estimates, dependency types, working lags, resource names, maximum units and multiple positive-percentage assignments. Fixed-work data is read where it can be represented.
- Fixed-units tasks are approximated as fixed-duration and reported. Zero-percentage assignments are reported and omitted. Duplicate resource names from MPP are made distinct and reported. Unsupported summary links or unavailable lag units are reported and omitted; not all losses can be detected.
- The reader may skip unscheduled tasks. Resource/task calendars, rates, costs, timephased data, progress, baselines, custom fields, constraints, manual scheduling and other Project-specific information are not comprehensively preserved.
- **Confirmed import defect:** an MPP task with a 16 November 2026 13:00–17:00 start-no-earlier-than constraint was imported as 08:00–12:00. The general import warning appeared, but the per-task loss report was empty. Imported start timestamps are reduced to dates and treated as earliest-start dates; even originally unconstrained linked tasks may therefore fail to move earlier after their predecessors change. Fixing this is a priority before relying on imported schedules.

## 7. Verification and next priorities

- The automated pytest suite covers dependency cases, resource calculations, `.pxplan` migration, UI behavior and pymppwriter MPP read-back. Passing tests do not establish Microsoft Project compatibility.
- An independent MPXJ-based reader has also read sample exported MPP resource and fixed-work fields. It did not recalculate those plans in Microsoft Project. Microsoft Project round-trip/recalculation validation remains outstanding.
- Recommended next work, not yet agreed as implemented scope: preserve imported times, constraints and manual/automatic state; report any conversions that alter a schedule; build representative Microsoft Project-created fixtures and compare after Project recalculates; then expand calendars, fixed-units/effort-driven semantics and other scheduling features based on demonstrated needs.
- Do not treat OnePyProject as a lossless editor or rely on imported/exported schedules for important decisions until the relevant data and calculations have been validated in Microsoft Project.
