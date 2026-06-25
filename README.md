# TEAM MANAGEMENT PORTAL

Flask + SQLite app: roster-based leave requests, manager **leave tracker**, optional **Scrum dashboard** per team, approvals, CSV exports (with submitter IP), and name typeahead (`/api/employees?q=`).

## Features

| Area | Notes |
|------|--------|
| Roster & teams | SQLite `teams` + `team_roster` (seeded from `EMPLOYEES` as **Default** on first run). **Settings** → **Create/Update Team**: upload **.xlsx** or UTF-8 CSV with **TeamName** / **EmployeeName** (legacy **Team** / **Name** still work). With **Default** selected in the nav **Team** picker, the active roster is the **union of everyone on every team** (distinct names). **Export roster mapping (.xlsx)** downloads every team–member row from the DB, plus **Default** rows for seeded employees who are not on any team. **Team** modal switches the active roster. Each team has a **hub mode** (`leave` or `scrum`) on the Scrum page. Leave tracker, Scrum, Meet, Nokia compare, and exports use that roster |
| Leave | Types PL / UL / SL / LL and **CompOFF** (shown on the grid but not counted in leave day-units or capacity); first & last day; half-day only when single day; IP logged. Employees apply via **`/portal/leave/apply`** (after sign-in). **`/leave`** is **manager-only** (dashboard secret session) for Meet / manual roster entries |
| Employee portal | **`/`** — **Microsoft** (optional **`MICROSOFT_OAUTH_*`**) and/or **SMTP email OTP** (**`PORTAL_OTP_SMTP_HOST`** + **`PORTAL_OTP_FROM`**). If neither is set and **`TEAM_TRACKER_PRODUCTION`** is **not** enabled, the app **auto-enables** dev email OTP (code in flash + server log) so local clones work without editing `.env`. Set **`TEAM_TRACKER_PRODUCTION=1`** for real deployments and then configure OAuth or SMTP (or explicit **`PORTAL_OTP_DEV_CONSOLE=1`**). **`.env` in the project root** (same folder as **`main.py`**) is loaded with priority over empty inherited env vars. Roster: **`nokia_portal_roster.py`**. **Manager** sign-in: **`MANAGER_DASHBOARD_PASSWORD`**; optional **LPO/SM** sign-in (**`LPO_SM_DASHBOARD_PASSWORD`**, same rights) — both post to **`/dashboard`**. Hub **`/portal`**. Azure redirect `{PUBLIC_URL}/auth/microsoft/callback`. Optional **`PORTAL_SEED_DEMO_ITEMS=1`**. |
| My Leave Status | **`/my-requests`** requires a **portal** session (Microsoft or email OTP); shows that roster identity’s leave rows only |
| My sprint board | **`/portal/my-sprint/board`** redirects to **`/portal/sprint/<sprint_id>/board`** for your latest sprint with assigned items (same sticky Kanban UI as managers, scoped to **your** rows). Legacy **`/portal/sprint`** GET does the same. **Changes are queued** (moves, notes, adds, edits, checklist updates, deletes); the live sprint board updates only after a manager approves them under **`/scrum/portal-proposals`** or **inline on the sprint team overview** (amber cards under each person’s **Sprint burnt** line). Employees can add optional **proof URLs** (screenshots, docs) on moves, stand-up saves, and Done edits — managers see them in the review dialog. Approved updates are written to **`scrum_item_activity`** with an **`[approved employee change]`** prefix on the note text |
| Scrum | `/scrum` **Sprint hub** — **Full team JSON bundle** (`/scrum/team-bundle.json` download + import on the hub): exports **roster**, **hub mode**, **task kinds**, **all sprints**, **stickies** (including activity, goals, daily tasks, appreciation, portal proposals) for the **currently selected team**; **import** replaces that entire Scrum dataset on the **active** team (another manager switches team in Settings first). **Create sprints** (pick **start** only; **end** is always **14 calendar days** from that start, inclusive — a fixed two-week sprint); **Create next sprint** (after the first sprint: **start** = day after the latest sprint’s **end**, same 14-day window and **Do** / **In progress** carry-over as manual create; **Name** defaults by incrementing the trailing digits of the latest sprint name, e.g. **FRONTIER-FB2612** → **FRONTIER-FB2613**); **Sprint export**: **Download .xlsx** for a selected sprint (tabs include **Summary** — sprint name, total capacity, metrics in **A:B**; **leave tracker** from **column D** (sprint dates × roster); member sprint goals; team task kinds; a **PNG snapshot** tab named after the sprint (Sprint hub–style view); **SprintStatus** — FB2610-style sticky rows with **day-wise** logged hours; **HPPM** — same **HPPM view** summary (estimate / burnt by work type, %) plus **all stickies** with **Burnt %**; **Daily tasks Details** — **Team** + **Member**, day-wise hours, compact sticky rows, daily task rows; **Appriciation** — appreciation **author**, **comment**, sticky **title**, **assignee** in sheet columns **C, D, G, H** only). **Summary** adds **Sprint leaves (h)** (weekday leave in the sprint window, same basis as HPPM absences / leave tracker), **planned capacity %** ((sum estimates + Sprint leaves) ÷ sprint capacity), and **free sprint capacity** (sprint capacity − sum estimates − Sprint leaves, hours). **New sprints** carry **Do** and **In progress** stickies from the latest prior sprint that **ended before** the new start date into the new sprint’s **backlog**, same assignee and fields, **keeping the same estimate hours**; each carried sticky is a **new row** so **Burnt hours start at 0** (prior sprint activity is not copied). **Create sprint** shows **Sprint total capacity (h)**: sum over the team roster of Mon–Fri **8h** per day in the sprint window **minus** approved/pending **leave** (same rules as the sticky-board capacity strip); value is stored on the sprint and refreshed when the sprint **start** is saved. **Team overview** (`/scrum/sprint/<id>`): **edit sprint start** anytime (**end** is always 14 calendar days from that start, inclusive); compact queue counts, per-member **Sprint burnt** (burnt hours vs estimated hours on all stickies when total estimate is positive); when that figure is **below 67%**, each person also sees **NDY**, **FSY**, **CODE**, **Improvement**, and **Process&Tools** **% burnt** (same ratio per sticky type for that assignee); **estimate vs Burnt** twin bars (red **stretch** when logged hours exceed total estimate), in-progress preview, latest activity. **Sticky board** (`/scrum/sprint/<id>/board?assignee=…`): dark UI; **add stickies only from Do** (dialog: title, **details**, DOD, required estimate, sticky **Type** is one of **NDY**, **CODE**, **FSY**, **Improvement**, or **Process&Tools**); drag between columns with **Burnt hours + note**; moves to **Done** can attach **artifact links** (edited later from Done). **Do → In progress** clears prior Burnt history on that sticky; stand-up updates accept **Burnt hours**. Canonical types in `scrum_team_task_kind` |
| Manager | `/dashboard` — access code from env; leave tracker month grid; **Sprint delivery** summary (recent sprints, % done, **Burnt hours from sticky moves**, links to open each sprint) |
| Meet (Leave Plan/DSM Attendance) | `/meet` — three-day strip (anchor ±1); center + today highlights; single-click empty cell to add leave; single-click approve / double-click remove per day; auto-promote when all days approved |
| Leave Reports (`/reports`) | Date range, leave table, optional attendance; **Export** downloads **`.xlsx`**: **Summary** (tracker totals per person); **Request detail** (leave type, status, calendar span, working days overlapping the range, tracker day-units taken in range, submitted timestamp, from/to dates, duration, comma-separated leave dates counted in range, codes); **Leave dates** (per-day tracker rows); **one worksheet per roster member** with a short summary plus that person’s request lines. **`/reports/export.csv`** remains. Audit log download |
| Ops | `/healthz`, `wsgi.py`, pytest |

## Quick start

```powershell
cd <this-folder>
copy .env.example .env
# Edit .env in the project root (loaded with priority over empty process env).
# Set FLASK_SECRET_KEY, MANAGER_DASHBOARD_PASSWORD, and for production TEAM_TRACKER_PRODUCTION=1 plus Microsoft or SMTP OTP.
# Optional: LPO_SM_DASHBOARD_PASSWORD — second access code; same sign-in flow and manager privileges; must differ from the manager code.
# Local: leave TEAM_TRACKER_PRODUCTION unset — dev email OTP turns on automatically if OAuth/SMTP are not set.
python -m pip install -r requirements.txt
python main.py
```

Open **http://127.0.0.1:5000**.

### Important environment variables

| Variable | Purpose |
|----------|---------|
| `FLASK_SECRET_KEY` | **Required in production** — sessions + CSRF |
| `MANAGER_DASHBOARD_PASSWORD` | Manager sign-in for leave tracker, `/scrum`, `/meet`, `/reports`, and all CSV downloads (email + password on **`/login`**, or legacy access-code flow where still enabled). Stored only as a PBKDF2 hash when creating manager rows — never logged. |
| `PRIMARY_OWNER_MANAGER_EMAIL` | **Optional.** On first DB init only: if set to a valid email and that address is not yet in `managers`, a row is created with **`MANAGER_DASHBOARD_PASSWORD` as the initial password** (hashed) and **Default** (or first) team. Omit to skip this auto-seed; use **`/register`** or SQL to add managers instead. |
| `LPO_SM_DASHBOARD_PASSWORD` | **LPO/SM** row on `/` and `/dashboard`: same **access-code** POST as manager (`secret_code` + `gate_kind=lpo_sm`), same session and **all manager routes**. Use a **different** secret than `MANAGER_DASHBOARD_PASSWORD`. Omit or leave empty to disable that sign-in card |
| `TEAM_TRACKER_DB_PATH` | SQLite path (default `./data/team_tracker.db`). On each roster **.xlsx/CSV** upload (Create/Update Team), a copy is saved next to it as `team_tracker.pre_roster_upload_YYYYMMDD_HHMMSS.db`. Stop the app, back up the current DB, then replace `team_tracker.db` with that snapshot (renamed) to undo roster membership. **Leave rows** are not removed by roster upload. For data from before this snapshot feature, restore your own backup, Windows **Previous Versions** on the `data` folder, or re-upload **Export roster mapping (.xlsx)**. |
| `TEAM_TRACKER_DB_BACKUP_INTERVAL_HOURS` | While **`python main.py`** (or any process running `create_app()`) is up, copy the live DB into **`Backup/`** under the project root on this interval using SQLite’s online backup (default **2** hours). Set to **`0`** to turn off the in-process scheduler. Also run **`python team_tracker_backup.py --once`** from the project folder for Task Scheduler / cron. Optional: **`TEAM_TRACKER_DB_BACKUP_KEEP`** (default **30**) — oldest timestamped backups are deleted after each run. |
| `WTF_CSRF_ENABLED` | Set `false` only in automated tests |
| `TESSERACT_CMD` | Optional — full path to `tesseract.exe` if OCR cannot find Tesseract |
| `TEAM_TRACKER_AUTO_TESSERACT` | Windows default `1`: if Tesseract is missing, the app runs a one-time silent `winget install UB-Mannheim.TesseractOCR`. Set `0` to disable (e.g. locked-down PCs or CI). |
| `TEAM_TRACKER_PRODUCTION` | Set **`1`** on real servers — disables auto dev email OTP until you configure Microsoft or SMTP (see Employee portal row) |
| `MICROSOFT_OAUTH_CLIENT_ID` / `MICROSOFT_OAUTH_CLIENT_SECRET` | Employee **Microsoft** sign-in on `/` |
| `PORTAL_OTP_SMTP_HOST`, `PORTAL_OTP_FROM`, … | **Email OTP** — real mail delivery (see `.env.example`) |
| `PORTAL_OTP_DEV_CONSOLE` | **`1`** forces dev OTP; **`0`** forces it off. If unset, dev OTP auto-enables locally when OAuth and SMTP are both unset and `TEAM_TRACKER_PRODUCTION` is not set |

Use strong secrets in production; keep `.env` out of version control and terminate TLS at your edge.

**Nokia e-tool vs leave tracker:** open **Nokia e-tool** from the leave tracker toolbar (`/worksheet/nokia-audit`). Paste or upload a screenshot; OCR uses [Tesseract](https://github.com/tesseract-ocr/tesseract). The app looks for `tesseract` on `PATH`, then `TESSERACT_CMD`, then typical Windows locations including **`%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe`** (the usual winget install path). On Windows, if nothing is found and **`TEAM_TRACKER_AUTO_TESSERACT`** is not `0`, it attempts a one-time silent install via **`winget install UB-Mannheim.TesseractOCR`**. `pip install -r requirements.txt` includes Pillow and pytesseract. The legacy URL `/reports/nokia-audit` still works.

## Docker & Compose

```powershell
docker compose up --build
```

Pass `MANAGER_DASHBOARD_PASSWORD` via environment or a Compose `.env` file.

## Tests

```powershell
python -m pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

To **remove a manager account** (login row only; teams and leave data stay): set `TEAM_TRACKER_DB_PATH` if needed, then run `python scripts/delete_manager_account.py you@example.com`.

## Layout

- `main.py` — local dev entry point (`create_app().run(...)`)
- `config.py` — environment / `.env` loading and `Flask` config from env vars
- `database/session.py` — SQLite `get_db(app)` factory
- `team_portal/views.py` — small blueprints (e.g. `/healthz`); larger route sets still live in `create_app` in `app.py` until migrated
- `team_portal/models.py` — canonical SQLite table name constants
- `app.py` — roster, fuzzy helpers, manager leave tracker + scrum builders, routes  
- `templates/dashboard.html`, `static/worksheet.css` — month grid + gate + edit list; **Export .xlsx** downloads the same month table (Name, Total, day codes) via `/dashboard/leave-tracker-month.xlsx`  
- `templates/scrum_hub.html`, `templates/scrum_sprint_team.html`, `templates/scrum_sprint_hppm.html`, `templates/scrum_sprint_leave_tracker.html`, `templates/scrum_kanban.html`, `static/scrum_board.css` — sprint hub (`/scrum`), team overview (`/scrum/sprint/<id>`) with **Type stack** (vertical) and **Sprint burndown** side by side in the hero; **Sprint capacity** on the sprint team hero is **recomputed from current leave** (not a stale snapshot), **synced** to `scrum_sprint.team_capacity_hours` when the sprint is open, **auto-refreshed** every 2 minutes from `/scrum/api/sprint-team-capacity`, and labeled **net, weekdays after leave**; **HPPM view** (`/scrum/sprint/<id>/hppm` or nav **HPPM view** when a sprint was opened) with **Type stack** as **horizontal** rows (SVG height aligned to **Area stack**) plus **Area stack** and HPPM summary table (per–work-type **estimate** and **burnt** hours; **% Team Sprint Estimate** = share of table estimate total (100% on Total row); **% Team Burnt** = share of table burnt total (100% on Total row); gross/net capacity as reference; **Total** row; **Absences** = weekday leave day-units × 8h (estimate column matches burnt for capacity lost to leave)) and task tables, sprint-window leave grid (`/scrum/sprint/<id>/leave-tracker`), sticky Kanban (`/scrum/sprint/<id>/board?assignee=…` and portal `/portal/sprint/<id>/board`) without per-board area stack, then sprint capacity  
- `templates/meet.html`, `static/meet.css` — Leave Plan/DSM Attendance grid  
- `templates/nokia_audit.html`, `static/nokia_audit.css` — Nokia e-tool screenshot OCR vs leave tracker (`/worksheet/nokia-audit`)  
- `leave_grid_image.py` — colored Nokia-style grid: date header + name rows + red/pink cell sampling  
- `static/name_autocomplete.js` — client typeahead  
- `wsgi.py` — Gunicorn entry  
- `Not Relevant Files/` — optional local folder for SQL dumps, old DB snapshots, scratch DBs, duplicate tree **`SCRUM_vF.1`**, and `standalone.html` (see `Not Relevant Files/README.md`); ignored by git by default

## Standalone

Legacy static demo: **`Not Relevant Files/legacy_static_demo/standalone.html`** (open in a browser; no Flask leave tracker).
