"""Tests for the team tracker website."""

from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone

import pytest

from app import (
    EMPLOYEES,
    create_app,
    fuzzy_employee_matches,
    get_db,
    parse_nokia_grid_combined,
    parse_nokia_grid_tsv,
    parse_nokia_grid_whitespace,
    resolve_employee_name,
)


def test_roster_fuzzy():
    assert "Shaishta Anjum" in fuzzy_employee_matches("shais", 5)
    assert resolve_employee_name("shaishta anjum") == "Shaishta Anjum"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_TRACKER_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("MANAGER_DASHBOARD_PASSWORD", "mgrpw")
    monkeypatch.setenv("WTF_CSRF_ENABLED", "false")
    monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "test-ms-client-id")
    monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_SECRET", "test-ms-client-secret")
    application = create_app()
    application.config["TESTING"] = True
    monkeypatch.setenv("TEAM_TRACKER_AUTO_TESSERACT", "0")
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def _manager_login(c):
    res = c.post("/dashboard", data={"gate_kind": "manager", "secret_code": "mgrpw"}, follow_redirects=False)
    assert res.status_code in (302, 303)


def test_healthz(client):
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.get_json() == {"status": "ok"}


def test_api_employees_requires_query(client):
    assert client.get("/api/employees?q=").get_json()["matches"] == []


def test_api_employees_match(client):
    data = client.get("/api/employees?q=var").get_json()
    assert "Varshini Raj" in data["matches"]


def test_home_ok(client):
    res = client.get("/", follow_redirects=True)
    assert res.status_code == 200
    assert b"TEAM PORTAL" in res.data
    assert b"Employee Portal" in res.data or b"Verify Code" in res.data
    assert b"Manager Access" in res.data


def test_home_shows_email_otp_when_smtp_only(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_TRACKER_DB_PATH", str(tmp_path / "otp_home.db"))
    monkeypatch.setenv("MANAGER_DASHBOARD_PASSWORD", "mgrpw")
    monkeypatch.setenv("WTF_CSRF_ENABLED", "false")
    monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("PORTAL_OTP_SMTP_HOST", "localhost")
    monkeypatch.setenv("PORTAL_OTP_FROM", "noreply@example.test")
    application = create_app()
    application.config["TESTING"] = True
    c = application.test_client()
    r = c.get("/", follow_redirects=True)
    assert r.status_code == 200
    assert b"Employee Portal" in r.data or b"Continue as Employee" in r.data


def test_home_auto_enables_dev_otp_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_TRACKER_DB_PATH", str(tmp_path / "auto_otp_home.db"))
    monkeypatch.setenv("MANAGER_DASHBOARD_PASSWORD", "mgrpw")
    monkeypatch.setenv("WTF_CSRF_ENABLED", "false")
    monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("PORTAL_OTP_SMTP_HOST", raising=False)
    monkeypatch.delenv("PORTAL_OTP_FROM", raising=False)
    monkeypatch.delenv("PORTAL_OTP_DEV_CONSOLE", raising=False)
    monkeypatch.delenv("TEAM_TRACKER_PRODUCTION", raising=False)
    application = create_app()
    application.config["TESTING"] = True
    r = application.test_client().get("/", follow_redirects=True)
    assert r.status_code == 200
    assert b"Employee Portal" in r.data or b"Continue as Employee" in r.data


def test_home_employee_signin_off_when_production_without_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_TRACKER_DB_PATH", str(tmp_path / "prod_signin_home.db"))
    monkeypatch.setenv("MANAGER_DASHBOARD_PASSWORD", "mgrpw")
    monkeypatch.setenv("WTF_CSRF_ENABLED", "false")
    monkeypatch.setenv("TEAM_TRACKER_PRODUCTION", "1")
    monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("PORTAL_OTP_SMTP_HOST", raising=False)
    monkeypatch.delenv("PORTAL_OTP_FROM", raising=False)
    monkeypatch.setenv("PORTAL_OTP_DEV_CONSOLE", "0")
    application = create_app()
    application.config["TESTING"] = True
    r = application.test_client().get("/", follow_redirects=True)
    assert r.status_code == 200
    assert b"Employee Portal" in r.data


def test_portal_email_otp_send_verify(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_TRACKER_DB_PATH", str(tmp_path / "otp_flow.db"))
    monkeypatch.setenv("MANAGER_DASHBOARD_PASSWORD", "mgrpw")
    monkeypatch.setenv("WTF_CSRF_ENABLED", "false")
    monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("PORTAL_OTP_SMTP_HOST", "localhost")
    monkeypatch.setenv("PORTAL_OTP_FROM", "noreply@example.test")
    application = create_app()
    application.config["TESTING"] = True
    c = application.test_client()

    captured: list[tuple[str, str]] = []

    def fake_send(app, to_addr, code):
        captured.append((to_addr, code))

    monkeypatch.setattr("app.send_portal_otp_smtp", fake_send)

    r = c.post("/auth/email-otp/send", data={"email": "akanksha.jha@nokia.com"})
    assert r.status_code in (302, 303)
    assert captured and captured[0][0] == "akanksha.jha@nokia.com"
    code = captured[0][1]

    r2 = c.post("/auth/email-otp/verify", data={"code": code}, follow_redirects=False)
    assert r2.status_code in (302, 303)
    assert "/portal" in (r2.headers.get("Location") or "")

    dash = c.get("/portal")
    assert dash.status_code == 200
    assert b"My sprint work" in dash.data


def test_portal_email_otp_dev_console_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_TRACKER_DB_PATH", str(tmp_path / "otp_dev.db"))
    monkeypatch.setenv("MANAGER_DASHBOARD_PASSWORD", "mgrpw")
    monkeypatch.setenv("WTF_CSRF_ENABLED", "false")
    monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("PORTAL_OTP_SMTP_HOST", raising=False)
    monkeypatch.delenv("PORTAL_OTP_FROM", raising=False)
    monkeypatch.delenv("PORTAL_OTP_DEV_CONSOLE", raising=False)
    monkeypatch.delenv("TEAM_TRACKER_PRODUCTION", raising=False)
    application = create_app()
    application.config["TESTING"] = True
    c = application.test_client()
    r = c.post(
        "/auth/email-otp/send",
        data={"email": "akanksha.jha@nokia.com"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    m = re.search(rb"your code is (\d{6})", r.data)
    assert m
    code = m.group(1).decode()
    r2 = c.post("/auth/email-otp/verify", data={"code": code}, follow_redirects=False)
    assert r2.status_code in (302, 303)
    assert "/portal" in (r2.headers.get("Location") or "")


def test_leave_submit_via_portal_uses_roster_and_ip(client, app):
    with client.session_transaction() as sess:
        sess["portal_user"] = {
            "email": "akanksha.jha@nokia.com",
            "name": "Akanksha Jha",
            "roster_name": "Akanksha Jha",
            "role": "employee",
        }
    res = client.post(
        "/portal/leave/apply",
        data={
            "leave_kind": "pl",
            "start_date": "2026-07-01",
            "end_date": "2026-07-01",
            "reason": "Test day off",
        },
        environ_base={"REMOTE_ADDR": "203.0.113.9"},
        follow_redirects=False,
    )
    assert res.status_code in (302, 303)
    assert "/portal" in (res.headers.get("Location") or "")
    conn = get_db(app)
    row = conn.execute(
        "SELECT employee_name, submitted_ip FROM leave_requests ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row["employee_name"] == "Akanksha Jha"
    assert row["submitted_ip"] == "203.0.113.9"


def test_dashboard_gate_shows_without_password_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_TRACKER_DB_PATH", str(tmp_path / "nogate.db"))
    monkeypatch.setenv("MANAGER_DASHBOARD_PASSWORD", "")
    monkeypatch.setenv("WTF_CSRF_ENABLED", "false")
    monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_SECRET", raising=False)
    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert b"Manager access is not configured" in r.data or b"Manager access" in r.data
    assert b"Access code" in r.data or b"gate" in r.data.lower()


def test_portal_flow_with_fake_session(client, app):
    with client.session_transaction() as sess:
        sess["portal_user"] = {
            "email": "akanksha.jha@nokia.com",
            "name": "Akanksha Jha",
            "roster_name": "Akanksha Jha",
            "role": "employee",
        }
    dash = client.get("/portal")
    assert dash.status_code == 200
    assert b"My sprint work" in dash.data
    rv = client.post(
        "/portal/leave/apply",
        data={
            "leave_kind": "pl",
            "start_date": "2026-06-02",
            "end_date": "2026-06-04",
            "reason": "Time off",
        },
        follow_redirects=True,
    )
    assert rv.status_code == 200
    hist = client.get("/my-requests")
    assert hist.status_code == 200
    assert b"Time off" in hist.data


def test_portal_leave_rejects_weekend_start(client, app):
    with client.session_transaction() as sess:
        sess["portal_user"] = {
            "email": "sumit.patra@nokia.com",
            "name": "Sumit Patra",
            "roster_name": "Sumit Patra",
            "role": "employee",
        }
    res = client.post(
        "/portal/leave/apply",
        data={
            "leave_kind": "sl",
            "start_date": "2026-06-06",
            "end_date": "2026-06-08",
            "reason": "Bad range",
        },
    )
    assert res.status_code == 200
    assert b"weekend" in res.data.lower()


def test_portal_requires_login(client):
    assert client.get("/portal", follow_redirects=False).status_code == 302


def test_portal_my_requests_blocks_name_change_post(client, app):
    with client.session_transaction() as sess:
        sess["portal_user"] = {
            "email": "akanksha.jha@nokia.com",
            "name": "Akanksha Jha",
            "roster_name": "Akanksha Jha",
            "role": "employee",
        }
    res = client.post(
        "/my-requests",
        data={"employee_name": "Sumit Patra"},
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert b"only view" in res.data.lower() or b"own requests" in res.data.lower()


def test_portal_leave_redirects_legacy_leave(client, app):
    with client.session_transaction() as sess:
        sess["portal_user"] = {
            "email": "akanksha.jha@nokia.com",
            "name": "Akanksha Jha",
            "roster_name": "Akanksha Jha",
            "role": "employee",
        }
    r = client.get("/leave", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "/portal/leave/apply" in (r.headers.get("Location") or "")


def test_leave_submit_manager_session_uses_roster_and_ip(client, app):
    _manager_login(client)
    name = EMPLOYEES[0]
    res = client.post(
        "/leave",
        data={
            "employee_name": name.lower(),
            "reason": "pl",
            "start_date": "2026-07-01",
            "end_date": "2026-07-01",
            "day_part": "full",
        },
        environ_base={"REMOTE_ADDR": "203.0.113.9"},
        follow_redirects=False,
    )
    assert res.status_code == 302
    assert "/my-requests" in (res.headers.get("Location") or "")
    conn = get_db(app)
    row = conn.execute(
        "SELECT employee_name, submitted_ip FROM leave_requests ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row["employee_name"] == name
    assert row["submitted_ip"] == "203.0.113.9"


def test_dashboard_wrong_secret(client):
    res = client.post("/dashboard", data={"gate_kind": "manager", "secret_code": "nope"})
    assert res.status_code == 200
    assert b"Invalid code" in res.data


def test_lpo_sm_signin_opens_leave_tracker(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_TRACKER_DB_PATH", str(tmp_path / "lpo_signin.db"))
    monkeypatch.setenv("MANAGER_DASHBOARD_PASSWORD", "mgrx")
    monkeypatch.setenv("LPO_SM_DASHBOARD_PASSWORD", "lponsecret")
    monkeypatch.setenv("WTF_CSRF_ENABLED", "false")
    monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_SECRET", raising=False)
    application = create_app()
    application.config["TESTING"] = True
    c = application.test_client()
    rv = c.post("/dashboard", data={"gate_kind": "lpo_sm", "secret_code": "lponsecret"}, follow_redirects=False)
    assert rv.status_code in (302, 303)
    with c.session_transaction() as sess:
        assert sess.get("manager") is True
        assert sess.get("manager_role") == "lpo_sm"
    dash = c.get("/dashboard?year=2026&month=7")
    assert dash.status_code == 200
    assert b"Leave tracker" in dash.data


def test_lpo_sm_wrong_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_TRACKER_DB_PATH", str(tmp_path / "lpo_wrong.db"))
    monkeypatch.setenv("MANAGER_DASHBOARD_PASSWORD", "mgrx")
    monkeypatch.setenv("LPO_SM_DASHBOARD_PASSWORD", "goodlpo")
    monkeypatch.setenv("WTF_CSRF_ENABLED", "false")
    monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_SECRET", raising=False)
    application = create_app()
    application.config["TESTING"] = True
    c = application.test_client()
    res = c.post("/dashboard", data={"gate_kind": "lpo_sm", "secret_code": "badlpo"})
    assert res.status_code == 200
    assert b"Invalid code" in res.data


def test_lpo_sm_rejects_manager_code(tmp_path, monkeypatch):
    """Manager password must not unlock the LPO/SM gate."""
    monkeypatch.setenv("TEAM_TRACKER_DB_PATH", str(tmp_path / "lpo_mix.db"))
    monkeypatch.setenv("MANAGER_DASHBOARD_PASSWORD", "onlymgr")
    monkeypatch.setenv("LPO_SM_DASHBOARD_PASSWORD", "onlylpo")
    monkeypatch.setenv("WTF_CSRF_ENABLED", "false")
    monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_SECRET", raising=False)
    application = create_app()
    application.config["TESTING"] = True
    c = application.test_client()
    res = c.post("/dashboard", data={"gate_kind": "lpo_sm", "secret_code": "onlymgr"})
    assert res.status_code == 200
    assert b"Invalid code" in res.data


def test_manager_reports_and_export(client):
    c = client
    assert c.get("/reports").status_code == 302

    _manager_login(c)

    r = c.get("/reports?start=2026-07-01&end=2026-07-31")
    assert r.status_code == 200
    assert b"Leave Reports" in r.data

    exp = c.get("/reports/export.csv?start=2026-07-01&end=2026-07-31")
    assert exp.status_code == 200
    assert b"submitted_ip" in exp.data

    xlsx = c.get("/reports/export-leaves.xlsx?start=2026-07-01&end=2026-07-31")
    assert xlsx.status_code == 200
    assert xlsx.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert xlsx.data[:2] == b"PK"
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(xlsx.data))
    assert "Summary" in wb.sheetnames
    assert "Request detail" in wb.sheetnames
    assert "Leave dates" in wb.sheetnames
    assert len(wb.sheetnames) >= 4
    ws_det = wb["Request detail"]
    assert ws_det.cell(row=1, column=3).value == "leave_type"
    assert ws_det.cell(row=1, column=13).value == "leaves_taken_dates_in_range"

    audit = c.get("/manager/audit.csv")
    assert audit.status_code == 200
    assert b"submitted_ip" in audit.data


def test_dashboard_leave_tracker_renders(client):
    c = client
    _manager_login(c)
    r = c.get("/dashboard?year=2026&month=7")
    assert r.status_code == 200
    assert b"worksheet-wrap" in r.data
    assert b"ws-weekend-head" in r.data
    assert b"Leave tracker" in r.data


def test_manager_main_nav_home_links_to_scrum_hub(client):
    c = client
    _manager_login(c)
    r = c.get("/dashboard?year=2026&month=7")
    assert r.status_code == 200
    html = r.data.decode("utf-8", errors="replace")
    start = html.find('<nav class="main-nav"')
    assert start != -1
    end = html.find("</nav>", start)
    assert end != -1
    nav = html[start:end]
    h = nav.find(">Home</a>")
    assert h != -1
    pre = nav[max(0, h - 160) : h]
    assert 'href="/scrum"' in pre


def test_meet_redirects_without_manager(client):
    assert client.get("/meet", follow_redirects=False).status_code == 302


def test_meet_renders_for_manager(client):
    c = client
    _manager_login(c)
    r = c.get("/meet?d=2026-07-10")
    assert r.status_code == 200
    assert b"Leave Plan/DSM Attendance" in r.data
    assert b"meet-table" in r.data


def test_meet_leave_day_approve_remove_promote_and_quick_leave(client, app):
    c = client
    _manager_login(c)
    name = EMPLOYEES[0]
    c.post(
        "/leave",
        data={
            "employee_name": name.lower(),
            "reason": "pl",
            "start_date": "2026-07-09",
            "end_date": "2026-07-11",
            "day_part": "full",
        },
        follow_redirects=False,
    )
    conn = get_db(app)
    leave_id = int(conn.execute("SELECT id FROM leave_requests ORDER BY id DESC LIMIT 1").fetchone()["id"])
    assert conn.execute("SELECT status FROM leave_requests WHERE id = ?", (leave_id,)).fetchone()["status"] == "pending"
    conn.close()

    anchor = "2026-07-10"
    assert c.post(
        "/meet/approve-day",
        data={"leave_id": leave_id, "work_date": "2026-07-09", "anchor": anchor},
    ).get_json() == {"ok": True}

    conn = get_db(app)
    assert (
        conn.execute(
            "SELECT decision FROM meet_leave_day WHERE leave_id = ? AND work_date = ?",
            (leave_id, "2026-07-09"),
        ).fetchone()["decision"]
        == "approved"
    )
    assert conn.execute("SELECT status FROM leave_requests WHERE id = ?", (leave_id,)).fetchone()["status"] == "pending"
    conn.close()

    assert c.post(
        "/meet/approve-day",
        data={"leave_id": leave_id, "work_date": "2026-07-10", "anchor": anchor},
    ).get_json() == {"ok": True}
    assert c.post(
        "/meet/approve-day",
        data={"leave_id": leave_id, "work_date": "2026-07-11", "anchor": anchor},
    ).get_json() == {"ok": True}
    conn = get_db(app)
    assert conn.execute("SELECT status FROM leave_requests WHERE id = ?", (leave_id,)).fetchone()["status"] == "approved"
    conn.close()

    assert c.post(
        "/meet/remove-day",
        data={"leave_id": leave_id, "work_date": "2026-07-10", "anchor": anchor},
    ).get_json() == {"ok": True}

    r = c.post(
        "/meet/quick-leave",
        data={
            "employee_name": name,
            "work_date": "2026-07-10",
            "reason": "pl",
            "duration_type": "full",
            "anchor": anchor,
        },
    )
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}

    bad = c.post(
        "/meet/quick-leave",
        data={
            "employee_name": name,
            "work_date": "2020-01-01",
            "reason": "pl",
            "duration_type": "full",
            "anchor": anchor,
        },
    )
    assert bad.status_code == 400
    assert bad.get_json() == {"ok": False, "error": "out_of_window"}


def test_parse_nokia_grid_tsv_detects_leave():
    hdr = ["Emp ID", "Name", "Country"] + [str(d) for d in range(1, 32)]
    cells = ["99", "Raj, Varshini", "India"] + [""] * 31
    cells[12] = "L"
    text = "\t".join(hdr) + "\n" + "\t".join(cells)
    m, un, err = parse_nokia_grid_tsv(text, 2026, 5)
    assert err is None
    assert m is not None
    assert 10 in m["Varshini Raj"]
    assert not un


def test_parse_nokia_grid_combined_accepts_csv_export():
    """Quoted comma-separated rows (typical Excel CSV) normalize to TSV and parse."""
    import csv
    import io

    hdr = ["Emp ID", "Name", "Country"] + [str(d) for d in range(1, 32)]
    row = ["99", "Raj, Varshini", "India"] + [""] * 31
    row[12] = "L"
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(hdr)
    w.writerow(row)
    m, un, err = parse_nokia_grid_combined(buf.getvalue(), 2026, 5)
    assert err is None
    assert m is not None
    assert 10 in m["Varshini Raj"]
    assert not un


def test_parse_nokia_grid_whitespace_detects_leave():
    """Space-separated grid (OCR-style) after a header row with 1 2 3…"""
    hdr = "Emp Name " + " ".join(str(d) for d in range(1, 32))
    parts = ["99", "Raj,", "Varshini"] + ["."] * 31
    parts[3 + 9] = "L"  # day-of-month 10
    row = " ".join(parts)
    text = hdr + "\n" + row
    m, un, err = parse_nokia_grid_whitespace(text, 2026, 5)
    assert err is None
    assert m is not None
    assert 10 in m["Varshini Raj"]
    assert not un


def test_nokia_audit_requires_manager(client):
    assert client.get("/worksheet/nokia-audit", follow_redirects=False).status_code == 302


def test_nokia_audit_year_month_query_and_month_names(client):
    c = client
    _manager_login(c)
    r = c.get("/worksheet/nokia-audit?year=2024&month=3")
    assert r.status_code == 200
    assert b"March" in r.data
    assert b"2024" in r.data


def test_team_roster_csv_upload_and_switch_worksheet(client, app):
    c = client
    _manager_login(c)
    csv_body = "Team,Name\nGamma,Only One\nGamma,Second Person\n"
    up = c.post(
        "/reports/roster-upload",
        data={"roster_csv": (io.BytesIO(csv_body.encode("utf-8")), "rosters.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert up.status_code == 200
    assert b"Roster updated" in up.data
    with app.app_context():
        conn = get_db(app)
        row = conn.execute("SELECT id FROM teams WHERE name = ?", ("Gamma",)).fetchone()
        conn.close()
    assert row
    gid = int(row["id"])
    sw = c.post(
        "/reports/team-select",
        data={"team_id": str(gid), "next": "/dashboard?year=2026&month=5"},
        follow_redirects=False,
    )
    assert sw.status_code in (302, 303)
    dash = c.get("/dashboard?year=2026&month=5")
    assert dash.status_code == 200
    assert b"Only One" in dash.data
    assert b"Second Person" in dash.data


def test_team_roster_xlsx_upload(client, app):
    from openpyxl import Workbook

    c = client
    _manager_login(c)
    bio = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.append(["TeamName", "EmployeeName"])
    ws.append(["XlsxOnly", "Only Xlsx Person"])
    wb.save(bio)
    bio.seek(0)
    up = c.post(
        "/reports/roster-upload",
        data={"roster_csv": (bio, "roster.xlsx")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert up.status_code == 200
    assert b"Roster updated" in up.data
    conn = get_db(app)
    row = conn.execute(
        "SELECT tr.employee_name FROM team_roster tr JOIN teams t ON t.id = tr.team_id WHERE t.name = ?",
        ("XlsxOnly",),
    ).fetchone()
    conn.close()
    assert row and row[0] == "Only Xlsx Person"


def test_reports_roster_export_xlsx_current_team(client, app):
    c = client
    _manager_login(c)
    csv_body = "TeamName,EmployeeName\nExportTestZ,Person A\nExportTestZ,Person B\n"
    c.post(
        "/reports/roster-upload",
        data={"roster_csv": (io.BytesIO(csv_body.encode("utf-8")), "rosters.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    conn = get_db(app)
    row = conn.execute("SELECT id FROM teams WHERE name = ?", ("ExportTestZ",)).fetchone()
    conn.close()
    assert row
    gid = int(row["id"])
    c.post(
        "/reports/team-select",
        data={"team_id": str(gid), "next": "/reports"},
        follow_redirects=True,
    )
    exp = c.get("/reports/roster-export.xlsx")
    assert exp.status_code == 200
    ct = exp.headers.get("Content-Type", "")
    assert "spreadsheetml" in ct or "officedocument" in ct
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(exp.data), read_only=True, data_only=True)
    try:
        ws = wb.active
        rows = [tuple(c for c in r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()
    assert rows[0] == ("TeamName", "EmployeeName")
    body_rows = [r for r in rows[1:] if any(x for x in r if x)]
    assert ("ExportTestZ", "Person A") in body_rows
    assert ("ExportTestZ", "Person B") in body_rows


def test_default_team_shows_union_of_all_rosters(client, app):
    c = client
    _manager_login(c)
    csv_body = "TeamName,EmployeeName\nGammaUnion,Gam Person Only\n"
    c.post(
        "/reports/roster-upload",
        data={"roster_csv": (io.BytesIO(csv_body.encode("utf-8")), "r.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    conn = get_db(app)
    default_id = int(conn.execute("SELECT id FROM teams WHERE lower(trim(name)) = 'default'").fetchone()["id"])
    conn.close()
    c.post(
        "/reports/team-select",
        data={"team_id": str(default_id), "next": "/dashboard?year=2026&month=1"},
        follow_redirects=True,
    )
    dash = c.get("/dashboard?year=2026&month=1")
    assert dash.status_code == 200
    html = dash.get_data(as_text=True)
    assert "Gam Person Only" in html
    assert EMPLOYEES[0] in html


def test_nokia_audit_post_compare(client):
    c = client
    _manager_login(c)
    name = EMPLOYEES[0]
    c.post(
        "/leave",
        data={
            "employee_name": name.lower(),
            "reason": "pl",
            "start_date": "2026-05-15",
            "end_date": "2026-05-15",
            "day_part": "full",
        },
        follow_redirects=False,
    )
    hdr = ["Emp ID", "Name", "Country"] + [str(d) for d in range(1, 32)]
    row = ["1", "Nope, Nobody", "India"] + [""] * 31
    grid = "\t".join(hdr) + "\n" + "\t".join(row)
    r = c.post(
        "/worksheet/nokia-audit",
        data={"year": "2026", "month": "5", "nokia_ocr_text": grid},
    )
    assert r.status_code == 200
    assert b"Defaulters" in r.data
    assert b"2026-05-15" in r.data
    assert b"Leave tracker" in r.data


def test_export_forbidden_without_manager(client):
    assert client.get("/reports/export.csv").status_code == 302
    assert client.get("/reports/export-leaves.xlsx").status_code == 302
    assert client.get("/reports/roster-export.xlsx").status_code == 302
    assert client.get("/reports/roster-export.csv", follow_redirects=False).status_code == 301


def test_scrum_sprint_default_two_week_end_inclusive():
    from datetime import date

    from app import scrum_sprint_default_end_date

    assert scrum_sprint_default_end_date(date(2026, 6, 5)) == date(2026, 6, 18)
    assert scrum_sprint_default_end_date(date(2026, 9, 1)) == date(2026, 9, 14)


def test_scrm_requires_manager(client):
    assert client.get("/scrum", follow_redirects=False).status_code == 302


def test_scrm_renders(client):
    c = client
    _manager_login(c)
    r = c.get("/scrum")
    assert r.status_code == 200
    assert b"Sprint hub" in r.data
    assert b"14 calendar days" in r.data or b"two-week" in r.data


def test_scrum_sprint_rename_from_hub(client, app):
    c = client
    _manager_login(c)
    c.post(
        "/scrum/sprint/create",
        data={
            "name": "Rename Me Sprint",
            "start_date": "2026-09-01",
            "end_date": "2026-09-10",
            "goal": "",
        },
        follow_redirects=True,
    )
    hub = c.get("/scrum")
    assert hub.status_code == 200
    assert b"scrum-sprint-rename-form" in hub.data
    assert b"/scrum/sprint/rename" in hub.data
    assert b"SprintLeaveView" in hub.data
    conn = get_db(app)
    sid = int(conn.execute("SELECT id FROM scrum_sprint WHERE name = ?", ("Rename Me Sprint",)).fetchone()["id"])
    conn.close()
    assert f"/scrum/sprint/{sid}/leave-tracker".encode() in hub.data
    c.post(
        "/scrum/sprint/rename",
        data={"sprint_id": str(sid), "name": "FB0052"},
        follow_redirects=True,
    )
    conn = get_db(app)
    nm = conn.execute("SELECT name FROM scrum_sprint WHERE id = ?", (sid,)).fetchone()["name"]
    conn.close()
    assert nm == "FB0052"


def test_scrum_sprint_rename_rejects_duplicate(client, app):
    c = client
    _manager_login(c)
    for nm in ("Sprint Alpha", "Sprint Beta"):
        c.post(
            "/scrum/sprint/create",
            data={
                "name": nm,
                "start_date": "2026-08-01",
                "end_date": "2026-08-14",
                "goal": "",
            },
            follow_redirects=True,
        )
    conn = get_db(app)
    sid_beta = int(conn.execute("SELECT id FROM scrum_sprint WHERE name = ?", ("Sprint Beta",)).fetchone()["id"])
    conn.close()
    c.post(
        "/scrum/sprint/rename",
        data={"sprint_id": str(sid_beta), "name": "Sprint Alpha"},
        follow_redirects=True,
    )
    conn = get_db(app)
    row = conn.execute("SELECT name FROM scrum_sprint WHERE id = ?", (sid_beta,)).fetchone()
    conn.close()
    assert row["name"] == "Sprint Beta"


def test_scrum_sprint_team_capacity_api_and_create_persists(client, app):
    c = client
    _manager_login(c)
    r = c.get("/scrum/api/sprint-team-capacity", query_string={"start_date": "2026-06-09", "end_date": "2026-06-09"})
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    n = len(EMPLOYEES)
    assert abs(float(j["hours"]) - 8.0 * n) < 0.05

    c.post(
        "/scrum/sprint/create",
        data={
            "name": "Cap Store Sprint",
            "start_date": "2026-06-09",
            "end_date": "2026-06-09",
            "goal": "",
        },
        follow_redirects=True,
    )
    conn = get_db(app)
    row = conn.execute(
        "SELECT team_capacity_hours FROM scrum_sprint WHERE name = ?", ("Cap Store Sprint",)
    ).fetchone()
    conn.close()
    assert row
    assert row["team_capacity_hours"] is not None
    cap_r = c.get(
        "/scrum/api/sprint-team-capacity",
        query_string={"start_date": "2026-06-09", "end_date": "2026-06-22"},
    )
    assert cap_r.status_code == 200
    expected = float(cap_r.get_json()["hours"])
    assert abs(float(row["team_capacity_hours"]) - expected) < 0.05


def test_scrum_sprint_team_delete_ui_has_modal_and_delete_post_works(client, app):
    c = client
    _manager_login(c)
    c.post(
        "/scrum/sprint/create",
        data={
            "name": "Delete Modal Sprint",
            "start_date": "2026-10-01",
            "end_date": "2026-10-14",
            "goal": "",
        },
        follow_redirects=True,
    )
    conn = get_db(app)
    sid = int(
        conn.execute("SELECT id FROM scrum_sprint WHERE name = ?", ("Delete Modal Sprint",)).fetchone()["id"]
    )
    conn.close()
    r = c.get("/scrum/sprint/%d" % sid)
    assert r.status_code == 200
    assert b'id="scrum-delete-sprint-dialog"' in r.data
    assert b'id="scrum-delete-sprint-open"' in r.data
    assert b'id="scrum-sprint-delete-form"' in r.data
    assert b"Delete permanently" in r.data
    del_r = c.post("/scrum/sprint/delete", data={"sprint_id": str(sid)}, follow_redirects=True)
    assert del_r.status_code == 200
    conn = get_db(app)
    gone = conn.execute("SELECT id FROM scrum_sprint WHERE id = ?", (sid,)).fetchone() is None
    conn.close()
    assert gone


def test_scrum_sprint_create_rejects_duplicate_name(client, app):
    c = client
    _manager_login(c)
    name = "Unique Dup Name Sprint 7f3a"
    c.post(
        "/scrum/sprint/create",
        data={
            "name": name,
            "start_date": "2026-09-01",
            "end_date": "2026-09-14",
            "goal": "",
        },
        follow_redirects=True,
    )
    r_dup = c.post(
        "/scrum/sprint/create",
        data={
            "name": name,
            "start_date": "2026-10-01",
            "end_date": "2026-10-14",
            "goal": "",
        },
        follow_redirects=True,
    )
    assert r_dup.status_code == 200
    assert b"already exists" in r_dup.data or b"different name" in r_dup.data
    r_case = c.post(
        "/scrum/sprint/create",
        data={
            "name": name.upper(),
            "start_date": "2026-11-01",
            "end_date": "2026-11-14",
            "goal": "",
        },
        follow_redirects=True,
    )
    assert r_case.status_code == 200
    assert b"already exists" in r_case.data or b"different name" in r_case.data
    conn = get_db(app)
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM scrum_sprint WHERE lower(trim(name)) = lower(trim(?))",
        (name,),
    ).fetchone()["c"]
    conn.close()
    assert int(n) == 1


def test_scrum_sprint_export_xlsx(client, app):
    from openpyxl import load_workbook

    c = client
    _manager_login(c)
    c.post(
        "/scrum/sprint/create",
        data={
            "name": "Export Sprint",
            "start_date": "2026-09-01",
            "end_date": "2026-09-14",
            "goal": "Excel check",
        },
        follow_redirects=True,
    )
    conn = get_db(app)
    sid = int(conn.execute("SELECT id FROM scrum_sprint WHERE name = ?", ("Export Sprint",)).fetchone()["id"])
    conn.close()
    c.post(
        "/scrum/sprint/item/add",
        data={
            "sprint_id": str(sid),
            "title": "Sticky A",
            "assignee": EMPLOYEES[0],
            "estimate_hours": "4",
            "kanban_column": "doing",
            "task_kind": "ndy",
            "notes": "Details here",
        },
        follow_redirects=True,
    )
    conn = get_db(app)
    iid = int(
        conn.execute(
            "SELECT id FROM scrum_sprint_item WHERE sprint_id = ? AND title = ?",
            (sid, "Sticky A"),
        ).fetchone()["id"]
    )
    conn.execute(
        """
        INSERT INTO scrum_item_activity (item_id, body, committed_hours, from_column, to_column, created_at)
        VALUES (?, 'Logged work', 1.5, 'doing', 'doing', '2026-09-02T10:00:00Z')
        """,
        (iid,),
    )
    conn.execute(
        """
        INSERT INTO scrum_item_appreciation (item_id, author, comment, created_at)
        VALUES (?, 'TestMgr', 'Great sprint contribution', '2026-09-03T12:00:00Z')
        """,
        (iid,),
    )
    conn.commit()
    conn.close()

    bad = c.post("/scrum/sprint/export-xlsx", data={"sprint_id": "999999"})
    assert bad.status_code in (302, 303)

    res = c.post("/scrum/sprint/export-xlsx", data={"sprint_id": str(sid)})
    assert res.status_code == 200
    assert res.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert res.data[:2] == b"PK"
    wb = load_workbook(io.BytesIO(res.data))
    assert wb.sheetnames[0] == "Summary"
    assert "SprintStatus" in wb.sheetnames
    assert "Activity log" in wb.sheetnames
    assert "Daily tasks Details" in wb.sheetnames
    assert "Appriciation" in wb.sheetnames
    ws0 = wb["Summary"]
    found_name_hdr = ws0.cell(row=2, column=4).value == "NAME"
    assert found_name_hdr
    found_sprint = False
    found_cap = False
    found_planned = False
    for r in range(1, 60):
        if ws0.cell(row=r, column=1).value == "Sprint name":
            assert ws0.cell(row=r, column=2).value == "Export Sprint"
            found_sprint = True
        if ws0.cell(row=r, column=1).value == "Total Sprint Capacity (h)":
            assert ws0.cell(row=r, column=2).value not in (None, "", "—")
            found_cap = True
        if ws0.cell(row=r, column=1).value == "Planned capacity (sum estimates ÷ sprint capacity, %)":
            found_planned = True
    assert found_sprint and found_cap and found_planned
    titles = [wb["SprintStatus"].cell(row=2, column=j).value for j in range(1, 5)]
    assert titles[2] == "Sticky A"
    hdr1 = [wb["SprintStatus"].cell(row=1, column=j).value for j in range(1, wb["SprintStatus"].max_column + 1)]
    dcol = hdr1.index("2026-09-02") + 1
    assert float(wb["SprintStatus"].cell(row=2, column=dcol).value or 0) >= 1.4
    act = wb["Activity log"]
    ah = [act.cell(row=1, column=j).value for j in range(1, act.max_column + 1)]
    dcol2 = ah.index("2026-09-02") + 1
    assert float(act.cell(row=2, column=dcol2).value or 0) >= 1.4
    wsd = wb["Daily tasks Details"]
    found_ov = False
    for row in range(1, 25):
        if wsd.cell(row=row, column=1).value and "Per-member sprint overview" in str(
            wsd.cell(row=row, column=1).value
        ):
            found_ov = True
            break
    assert found_ov
    wap = wb["Appriciation"]
    assert wap.cell(row=3, column=3).value == "Appriciation author"
    assert wap.cell(row=4, column=3).value == "TestMgr"
    assert wap.cell(row=4, column=4).value == "Great sprint contribution"
    assert wap.cell(row=4, column=7).value == "Sticky A"
    assert wap.cell(row=4, column=8).value == EMPLOYEES[0]


def test_team_hub_mode_updates_active_team(client, app):
    c = client
    _manager_login(c)
    res = c.post("/team/hub-mode", data={"hub_mode": "scrum", "next": "/scrum"}, follow_redirects=False)
    assert res.status_code in (302, 303)
    conn = get_db(app)
    row = conn.execute("SELECT hub_mode FROM teams ORDER BY id ASC LIMIT 1").fetchone()
    conn.close()
    assert row["hub_mode"] == "scrum"


def test_scrm_sprint_team_and_sticky(client, app):
    c = client
    _manager_login(c)
    c.post(
        "/scrum/sprint/create",
        data={
            "name": "Sprint A",
            "start_date": "2026-09-01",
            "end_date": "2026-09-14",
            "goal": "Ship widgets",
        },
        follow_redirects=True,
    )
    conn = get_db(app)
    sp = conn.execute("SELECT id FROM scrum_sprint WHERE name = ?", ("Sprint A",)).fetchone()
    assert sp
    sid = int(sp["id"])
    conn.close()
    team = c.get("/scrum/sprint/%d" % sid)
    assert team.status_code == 200
    assert b"Team" in team.data
    assert b"SprintLeaveView" in team.data
    assert ("/scrum/sprint/%d/leave-tracker" % sid).encode() in team.data
    lv = c.get("/scrum/sprint/%d/leave-tracker" % sid)
    assert lv.status_code == 200
    assert b"ws-table" in lv.data
    assert b"Leave tracker" in lv.data
    c.post(
        "/scrum/sprint/item/add",
        data={
            "sprint_id": str(sid),
            "title": "Story 1",
            "assignee": EMPLOYEES[0],
            "estimate_hours": "5",
            "kanban_column": "backlog",
            "task_kind": "story",
            "notes": "",
        },
        follow_redirects=True,
    )
    conn = get_db(app)
    n = int(conn.execute("SELECT COUNT(*) AS c FROM scrum_sprint_item WHERE sprint_id = ?", (sid,)).fetchone()["c"])
    conn.close()
    assert n >= 1
    board = c.get("/scrum/sprint/%d/board" % sid, query_string={"assignee": EMPLOYEES[0]})
    assert board.status_code == 200
    assert b"Sticky board" in board.data
    assert b"SprintLeaveView" in board.data
    assert ("/scrum/sprint/%d/leave-tracker" % sid).encode() in board.data
    assert b"ws-table" in board.data
    assert b"Available" in board.data


def test_scrum_kanban_capacity_strip_shows_daily_burnt_hours_including_weekend(client, app):
    """Sprint capacity grid shows per-calendar-day burnt hours (sticky activity), including weekends."""
    c = client
    _manager_login(c)
    c.post(
        "/scrum/sprint/create",
        data={
            "name": "DailyHoursStrip",
            "start_date": "2026-09-01",
            "end_date": "2026-09-10",
            "goal": "",
        },
        follow_redirects=True,
    )
    conn = get_db(app)
    sid = int(conn.execute("SELECT id FROM scrum_sprint WHERE name = ?", ("DailyHoursStrip",)).fetchone()["id"])
    emp = EMPLOYEES[0]
    c.post(
        "/scrum/sprint/item/add",
        data={
            "sprint_id": str(sid),
            "title": "StripCard",
            "assignee": emp,
            "estimate_hours": "4",
            "kanban_column": "doing",
            "task_kind": "ndy",
            "notes": "",
        },
        follow_redirects=True,
    )
    row = conn.execute(
        "SELECT id FROM scrum_sprint_item WHERE sprint_id = ? AND title = ?",
        (sid, "StripCard"),
    ).fetchone()
    assert row
    iid = int(row["id"])
    # 2026-09-06 is Sunday — still show logged hours in that column.
    conn.execute(
        """
        INSERT INTO scrum_item_activity (item_id, body, committed_hours, from_column, to_column, created_at)
        VALUES (?, '', 3.0, 'doing', 'doing', '2026-09-06T14:00:00Z')
        """,
        (iid,),
    )
    conn.commit()
    conn.close()
    board = c.get("/scrum/sprint/%d/board" % sid, query_string={"assignee": emp})
    assert board.status_code == 200
    assert b"scrum-ws-cell-effort" in board.data
    assert b"3.0h" in board.data
    assert b"Improvement" in board.data
    assert b"process_tools" in board.data


def test_scrum_sprint_team_shows_ndy_fsy_burn_when_overall_below_67(client, app):
    """Team overview shows NDY/FSY/CODE % burnt when overall Sprint burnt is under 67%."""
    c = client
    _manager_login(c)
    c.post(
        "/scrum/sprint/create",
        data={
            "name": "Kind Burn Sprint",
            "start_date": "2026-09-01",
            "end_date": "2026-09-14",
            "goal": "",
        },
        follow_redirects=True,
    )
    conn = get_db(app)
    sp = conn.execute("SELECT id FROM scrum_sprint WHERE name = ?", ("Kind Burn Sprint",)).fetchone()
    assert sp
    sid = int(sp["id"])
    conn.close()
    emp = EMPLOYEES[0]
    for title, kind in (("Ndy work", "ndy"), ("Fsy work", "fsy"), ("Code work", "code")):
        c.post(
            "/scrum/sprint/item/add",
            data={
                "sprint_id": str(sid),
                "title": title,
                "assignee": emp,
                "estimate_hours": "5",
                "kanban_column": "backlog",
                "task_kind": kind,
                "notes": "",
            },
            follow_redirects=True,
        )
    conn = get_db(app)
    ts = "2026-09-02T12:00:00Z"
    for title, hours in (("Ndy work", 1.0), ("Fsy work", 2.0), ("Code work", 0.5)):
        row = conn.execute(
            "SELECT id FROM scrum_sprint_item WHERE sprint_id = ? AND title = ?",
            (sid, title),
        ).fetchone()
        assert row
        iid = int(row["id"])
        conn.execute(
            """
            INSERT INTO scrum_item_activity (item_id, body, committed_hours, from_column, to_column, created_at)
            VALUES (?, '', ?, 'backlog', 'backlog', ?)
            """,
            (iid, hours, ts),
        )
    conn.commit()
    conn.close()
    team = c.get("/scrum/sprint/%d" % sid)
    assert team.status_code == 200
    html = team.get_data(as_text=True)
    assert "Cap:" in html
    assert "SCAPACITY" in html
    assert "scrum-member-kind-burn" in html
    assert "Sprint burnt" in html
    assert ">20%<" in html or "20%" in html  # NDY: 1/5
    assert ">40%<" in html or "40%" in html  # FSY: 2/5
    assert ">10%<" in html or "10%" in html  # CODE: 0.5/5
    assert "NDY" in html and "FSY" in html and "CODE" in html


def test_manager_team_bundle_export_import_roundtrip(client, app):
    import json
    from io import BytesIO

    c = client
    _manager_login(c)
    conn = get_db(app)
    tid = int(conn.execute("SELECT id FROM teams ORDER BY id LIMIT 1").fetchone()["id"])
    ts = "2026-10-01T12:00:00Z"
    cur = conn.execute(
        """
        INSERT INTO scrum_sprint (team_id, name, start_date, end_date, goal, created_at, updated_at)
        VALUES (?, 'BundleSprint', '2026-10-01', '2026-10-14', '', ?, ?)
        """,
        (tid, ts, ts),
    )
    sid = int(cur.lastrowid)
    cur2 = conn.execute(
        """
        INSERT INTO scrum_sprint_item
          (sprint_id, assignee, title, estimate_hours, status, notes, dod, done_artifacts, sort_order, created_at, updated_at, kanban_column, task_kind, area)
        VALUES (?, ?, 'Sticky A', 3.0, 'open', '', '', '[]', 0, ?, ?, 'backlog', 'ndy', '')
        """,
        (sid, EMPLOYEES[0], ts, ts),
    )
    iid = int(cur2.lastrowid)
    conn.execute(
        """
        INSERT INTO scrum_item_activity (item_id, body, committed_hours, from_column, to_column, created_at)
        VALUES (?, 'hello', 0.5, 'backlog', 'backlog', ?)
        """,
        (iid, ts),
    )
    conn.commit()
    conn.close()

    r = c.get("/scrum/team-bundle.json")
    assert r.status_code == 200
    bundle = json.loads(r.get_data(as_text=True))
    assert bundle["format"] == "team_tracker_team_bundle_v1"
    assert any(s.get("name") == "BundleSprint" for s in bundle["scrum_sprint"])

    conn = get_db(app)
    conn.execute("DELETE FROM scrum_sprint WHERE team_id = ? AND name = ?", (tid, "BundleSprint"))
    conn.commit()
    conn.close()

    buf = BytesIO(json.dumps(bundle).encode("utf-8"))
    buf.seek(0)
    r2 = c.post(
        "/scrum/team-bundle/import",
        data={"bundle_file": (buf, "team-bundle.json")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert r2.status_code == 200

    conn = get_db(app)
    row_sp = conn.execute(
        "SELECT id FROM scrum_sprint WHERE team_id = ? AND name = ?", (tid, "BundleSprint")
    ).fetchone()
    assert row_sp
    n_act = conn.execute(
        """
        SELECT COUNT(*) AS c FROM scrum_item_activity
        WHERE item_id IN (SELECT id FROM scrum_sprint_item WHERE sprint_id = ?)
        """,
        (int(row_sp["id"]),),
    ).fetchone()
    assert int(n_act["c"]) >= 1
    conn.close()


def test_scrum_sprint_team_highlights_activity_within_last_24h(client, app):
    """Latest activity lines from the last 24h get a distinct highlight class."""
    c = client
    _manager_login(c)
    c.post(
        "/scrum/sprint/create",
        data={
            "name": "Recent24h Sprint",
            "start_date": "2026-09-01",
            "end_date": "2026-09-14",
            "goal": "",
        },
        follow_redirects=True,
    )
    conn = get_db(app)
    sid = int(conn.execute("SELECT id FROM scrum_sprint WHERE name = ?", ("Recent24h Sprint",)).fetchone()["id"])
    emp = EMPLOYEES[0]
    c.post(
        "/scrum/sprint/item/add",
        data={
            "sprint_id": str(sid),
            "title": "ActivityCard",
            "assignee": emp,
            "estimate_hours": "2",
            "kanban_column": "doing",
            "task_kind": "ndy",
            "notes": "",
        },
        follow_redirects=True,
    )
    row = conn.execute(
        "SELECT id FROM scrum_sprint_item WHERE sprint_id = ? AND title = ?",
        (sid, "ActivityCard"),
    ).fetchone()
    assert row
    iid = int(row["id"])
    fresh_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds").replace("+00:00", "Z")
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn.execute(
        """
        INSERT INTO scrum_item_activity (item_id, body, committed_hours, from_column, to_column, created_at)
        VALUES (?, 'older sync', 0.5, 'doing', 'doing', ?)
        """,
        (iid, old_ts),
    )
    conn.execute(
        """
        INSERT INTO scrum_item_activity (item_id, body, committed_hours, from_column, to_column, created_at)
        VALUES (?, 'fresh sync', 1.0, 'doing', 'doing', ?)
        """,
        (iid, fresh_ts),
    )
    conn.commit()
    conn.close()
    team = c.get("/scrum/sprint/%d" % sid)
    assert team.status_code == 200
    html = team.get_data(as_text=True)
    assert "scrum-note-recent-24h" in html
    assert "scrum-note-card-recent-24h" in html


def test_unified_login_register_and_empty_workspace(tmp_path, monkeypatch):
    """Unified login: register manager account → sign in → empty workspace banner."""
    monkeypatch.setenv("TEAM_TRACKER_DB_PATH", str(tmp_path / "login_flow.db"))
    monkeypatch.setenv("MANAGER_DASHBOARD_PASSWORD", "mgrpw")
    monkeypatch.setenv("WTF_CSRF_ENABLED", "false")
    monkeypatch.setenv("TEAM_TRACKER_AUTO_TESSERACT", "0")
    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()

    r = c.get("/login")
    assert r.status_code == 200
    assert b"Employee Portal" in r.data
    assert b"Manager Access" in r.data
    assert b"Create one now" in r.data

    r = c.get("/register")
    assert r.status_code == 200
    assert b"Create Manager Account" in r.data

    r = c.post(
        "/register",
        data={"email": "new@test.com", "password": "secret1", "team_name": "TestTeam"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert b"Account created" in r.data
    assert b"Leave tracker" in r.data

    r2 = c.get("/dashboard")
    assert r2.status_code == 200
    assert b"Download roster template" in r2.data or b"Leave tracker" in r2.data

    c.post("/manager/logout", follow_redirects=True)

    r3 = c.post(
        "/login",
        data={"gate_kind": "manager", "email": "new@test.com", "password": "secret1"},
        follow_redirects=True,
    )
    assert r3.status_code == 200
    assert b"Signed in" in r3.data or b"Leave tracker" in r3.data

    r4 = c.post(
        "/login",
        data={"gate_kind": "manager", "email": "new@test.com", "password": "wrong"},
        follow_redirects=True,
    )
    assert b"Invalid email or password" in r4.data

    r5 = c.get("/reports/roster-template.xlsx")
    assert r5.status_code == 200
    assert b"PK" in r5.data[:4]


def test_primary_owner_manager_seeded_from_master_pin(tmp_path, monkeypatch):
    seed_email = "owner-seed-test@example.com"
    monkeypatch.setenv("TEAM_TRACKER_DB_PATH", str(tmp_path / "owner_seed.db"))
    monkeypatch.setenv("MANAGER_DASHBOARD_PASSWORD", "my_master_99")
    monkeypatch.setenv("PRIMARY_OWNER_MANAGER_EMAIL", seed_email)
    monkeypatch.setenv("WTF_CSRF_ENABLED", "false")
    monkeypatch.setenv("TEAM_TRACKER_AUTO_TESSERACT", "0")
    application = create_app()
    application.config["TESTING"] = True
    conn = get_db(application)
    row = conn.execute(
        "SELECT email, team_name FROM managers WHERE email = ? COLLATE NOCASE",
        (seed_email,),
    ).fetchone()
    conn.close()
    assert row
    assert row["email"].lower() == seed_email.lower()
    c = application.test_client()
    r = c.post(
        "/login",
        data={
            "gate_kind": "manager",
            "email": seed_email,
            "password": "my_master_99",
        },
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert b"Leave tracker" in r.data or b"Signed in" in r.data


def test_scrum_sprint_update_dates(client, app):
    c = client
    _manager_login(c)
    c.post(
        "/scrum/sprint/create",
        data={
            "name": "Date Edit Sprint",
            "start_date": "2026-10-01",
            "end_date": "2026-10-05",
            "goal": "",
        },
        follow_redirects=True,
    )
    conn = get_db(app)
    row = conn.execute("SELECT id FROM scrum_sprint WHERE name = ?", ("Date Edit Sprint",)).fetchone()
    assert row
    sid = int(row["id"])
    conn.close()
    r = c.post(
        "/scrum/sprint/update",
        data={"sprint_id": str(sid), "start_date": "2026-10-08", "end_date": "2026-10-22"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    conn = get_db(app)
    sp = conn.execute("SELECT start_date, end_date FROM scrum_sprint WHERE id = ?", (sid,)).fetchone()
    conn.close()
    assert str(sp["start_date"]).startswith("2026-10-08")
    assert str(sp["end_date"]).startswith("2026-10-21")


def test_scrum_sprint_carry_forward_do_and_doing(client, app):
    c = client
    _manager_login(c)
    conn = get_db(app)
    tid = int(conn.execute("SELECT id FROM teams ORDER BY id ASC LIMIT 1").fetchone()["id"])
    ts = "2026-01-01T00:00:00Z"
    conn.execute(
        """
        INSERT INTO scrum_sprint (team_id, name, start_date, end_date, goal, created_at, updated_at)
        VALUES (?, 'Carry Old', '2026-01-01', '2026-01-10', '', ?, ?)
        """,
        (tid, ts, ts),
    )
    old_sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    emp = EMPLOYEES[0]
    conn.execute(
        """
        INSERT INTO scrum_sprint_item
        (sprint_id, assignee, title, estimate_hours, status, notes, dod, sort_order, created_at, updated_at, kanban_column, task_kind, sticky_color_hex)
        VALUES (?, ?, 'Do task', 3, 'open', 'n1', 'd1', 0, ?, ?, 'do', 'ndy', NULL)
        """,
        (old_sid, emp, ts, ts),
    )
    do_iid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO scrum_sprint_item
        (sprint_id, assignee, title, estimate_hours, status, notes, dod, sort_order, created_at, updated_at, kanban_column, task_kind, sticky_color_hex)
        VALUES (?, ?, 'Doing task', 4, 'doing', 'n2', 'd2', 1, ?, ?, 'doing', 'code', '#112233')
        """,
        (old_sid, emp, ts, ts),
    )
    doing_iid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO scrum_item_activity (item_id, body, committed_hours, from_column, to_column, created_at)
        VALUES (?, '', 1.5, 'do', 'do', ?)
        """,
        (do_iid, ts),
    )
    conn.execute(
        """
        INSERT INTO scrum_item_activity (item_id, body, committed_hours, from_column, to_column, created_at)
        VALUES (?, '', 2.0, 'doing', 'doing', ?)
        """,
        (doing_iid, ts),
    )
    conn.commit()
    conn.close()
    r = c.post(
        "/scrum/sprint/create",
        data={"name": "Carry New", "start_date": "2026-01-15", "end_date": "2026-01-31", "goal": ""},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert b"carried" in r.data.lower()
    conn = get_db(app)
    new_sid = int(conn.execute("SELECT id FROM scrum_sprint WHERE name = ?", ("Carry New",)).fetchone()[0])
    rows = list(
        conn.execute(
            """
            SELECT title, kanban_column, assignee, notes, dod, task_kind, sticky_color_hex, estimate_hours
            FROM scrum_sprint_item WHERE sprint_id = ? ORDER BY title
            """,
            (new_sid,),
        )
    )
    conn.close()
    assert len(rows) == 2
    by_title = {x["title"]: x for x in rows}
    assert by_title["Do task"]["kanban_column"] == "backlog"
    assert by_title["Doing task"]["kanban_column"] == "backlog"
    assert by_title["Do task"]["notes"] == "n1"
    assert by_title["Doing task"]["notes"] == "n2"
    assert by_title["Do task"]["dod"] == "d1"
    assert by_title["Doing task"]["dod"] == "d2"
    assert by_title["Do task"]["task_kind"] == "ndy"
    assert by_title["Doing task"]["task_kind"] == "code"
    assert (by_title["Doing task"]["sticky_color_hex"] or "").lower() == "#112233"
    assert by_title["Do task"]["assignee"] == emp
    assert by_title["Doing task"]["assignee"] == emp
    assert abs(float(by_title["Do task"]["estimate_hours"]) - 1.5) < 0.001  # 3 - 1.5 logged
    assert abs(float(by_title["Doing task"]["estimate_hours"]) - 2.0) < 0.001  # 4 - 2 logged


def test_scrum_board_leave_strip_shows_stretch_when_overallocated(client, app):
    c = client
    _manager_login(c)
    c.post(
        "/scrum/sprint/create",
        data={
            "name": "Sprint Cap",
            "start_date": "2026-09-01",
            "end_date": "2026-09-14",
            "goal": "",
        },
        follow_redirects=True,
    )
    conn = get_db(app)
    sp = conn.execute("SELECT id FROM scrum_sprint WHERE name = ?", ("Sprint Cap",)).fetchone()
    assert sp
    sid = int(sp["id"])
    ts = "2026-08-15T12:00:00Z"
    conn.execute(
        """
        INSERT INTO leave_requests
        (employee_name, reason, description, start_date, end_date, duration_type, status, created_at, submitted_ip)
        VALUES (?, 'pl', '', '2026-09-08', '2026-09-10', 'multi', 'approved', ?, '')
        """,
        (EMPLOYEES[0], ts),
    )
    conn.execute(
        """
        INSERT INTO scrum_sprint_item
        (sprint_id, assignee, title, estimate_hours, status, notes, dod, sort_order, created_at, updated_at, kanban_column, task_kind)
        VALUES (?, ?, 'Big', 90, 'open', '', '', 0, ?, ?, 'do', 'ndy')
        """,
        (sid, EMPLOYEES[0], ts, ts),
    )
    conn.commit()
    conn.close()
    board = c.get("/scrum/sprint/%d/board" % sid, query_string={"assignee": EMPLOYEES[0]})
    assert board.status_code == 200
    assert b"Stretch" in board.data
    assert b"PL" in board.data


def test_scrm_api_move_updates_column(client, app):
    c = client
    _manager_login(c)
    conn = get_db(app)
    tid = int(conn.execute("SELECT id FROM teams ORDER BY id ASC LIMIT 1").fetchone()["id"])
    ts = "2026-11-01T00:00:00Z"
    conn.execute(
        """
        INSERT INTO scrum_sprint (team_id, name, start_date, end_date, goal, created_at, updated_at)
        VALUES (?, 'S Move', '2026-11-01', '2026-11-10', '', ?, ?)
        """,
        (tid, ts, ts),
    )
    sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO scrum_sprint_item
        (sprint_id, assignee, title, estimate_hours, status, notes, sort_order, created_at, updated_at, kanban_column, task_kind)
        VALUES (?, ?, 'Card', 1, 'open', '', 0, ?, ?, 'backlog', 'task')
        """,
        (sid, EMPLOYEES[0], ts, ts),
    )
    iid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    conn.close()
    r = c.post(
        "/scrum/api/item/move",
        json={
            "item_id": iid,
            "sprint_id": sid,
            "assignee": EMPLOYEES[0],
            "to_column": "doing",
            "committed_hours": "1.5",
            "note": "Started work",
        },
    )
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    conn = get_db(app)
    col = conn.execute("SELECT kanban_column, status FROM scrum_sprint_item WHERE id = ?", (iid,)).fetchone()
    conn.close()
    assert col["kanban_column"] == "doing"
    assert col["status"] == "doing"


def test_scrm_api_appreciation_add_and_delete_all(client, app):
    c = client
    _manager_login(c)
    conn = get_db(app)
    tid = int(conn.execute("SELECT id FROM teams ORDER BY id ASC LIMIT 1").fetchone()["id"])
    ts = "2026-11-01T00:00:00Z"
    conn.execute(
        """
        INSERT INTO scrum_sprint (team_id, name, start_date, end_date, goal, created_at, updated_at)
        VALUES (?, 'S Appr', '2026-11-01', '2026-11-10', '', ?, ?)
        """,
        (tid, ts, ts),
    )
    sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO scrum_sprint_item
        (sprint_id, assignee, title, estimate_hours, status, notes, sort_order, created_at, updated_at, kanban_column, task_kind)
        VALUES (?, ?, 'Appr card', 1, 'open', '', 0, ?, ?, 'doing', 'ndy')
        """,
        (sid, EMPLOYEES[0], ts, ts),
    )
    iid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    conn.close()
    r_add = c.post(
        "/scrum/api/item/appreciation",
        json={
            "item_id": iid,
            "sprint_id": sid,
            "assignee": EMPLOYEES[0],
            "comment": "Nice work",
        },
    )
    assert r_add.status_code == 200
    assert r_add.get_json()["ok"] is True
    conn = get_db(app)
    n1 = int(conn.execute("SELECT COUNT(*) AS c FROM scrum_item_appreciation WHERE item_id = ?", (iid,)).fetchone()["c"])
    conn.close()
    assert n1 == 1

    r_del = c.post(
        "/scrum/api/item/appreciation/delete-all",
        json={"item_id": iid, "sprint_id": sid, "assignee": EMPLOYEES[0]},
    )
    assert r_del.status_code == 200
    j = r_del.get_json()
    assert j["ok"] is True
    assert j["deleted"] == 1
    conn = get_db(app)
    n2 = int(conn.execute("SELECT COUNT(*) AS c FROM scrum_item_appreciation WHERE item_id = ?", (iid,)).fetchone()["c"])
    conn.close()
    assert n2 == 0


def test_scrm_api_move_to_done_saves_artifacts(client, app):
    import json

    c = client
    _manager_login(c)
    conn = get_db(app)
    tid = int(conn.execute("SELECT id FROM teams ORDER BY id ASC LIMIT 1").fetchone()["id"])
    ts = "2026-11-01T00:00:00Z"
    conn.execute(
        """
        INSERT INTO scrum_sprint (team_id, name, start_date, end_date, goal, created_at, updated_at)
        VALUES (?, 'S Done Art', '2026-11-01', '2026-11-10', '', ?, ?)
        """,
        (tid, ts, ts),
    )
    sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO scrum_sprint_item
        (sprint_id, assignee, title, estimate_hours, status, notes, sort_order, created_at, updated_at, kanban_column, task_kind)
        VALUES (?, ?, 'Ship it', 1, 'open', '', 0, ?, ?, 'doing', 'ndy')
        """,
        (sid, EMPLOYEES[0], ts, ts),
    )
    iid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    conn.close()
    r = c.post(
        "/scrum/api/item/move",
        json={
            "item_id": iid,
            "sprint_id": sid,
            "assignee": EMPLOYEES[0],
            "to_column": "done",
            "committed_hours": "0",
            "note": "wrapped",
            "artifacts": [{"label": "PR", "url": "https://example.com/pr/99"}],
        },
    )
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    conn = get_db(app)
    raw = conn.execute(
        "SELECT kanban_column, done_artifacts FROM scrum_sprint_item WHERE id = ?", (iid,)
    ).fetchone()
    conn.close()
    assert raw["kanban_column"] == "done"
    data = json.loads(raw["done_artifacts"])
    assert len(data) == 1
    assert data[0]["url"] == "https://example.com/pr/99"
    assert data[0]["label"] == "PR"


def test_scrm_api_move_to_done_rejects_bad_artifact_url(client, app):
    c = client
    _manager_login(c)
    conn = get_db(app)
    tid = int(conn.execute("SELECT id FROM teams ORDER BY id ASC LIMIT 1").fetchone()["id"])
    ts = "2026-11-01T00:00:00Z"
    conn.execute(
        """
        INSERT INTO scrum_sprint (team_id, name, start_date, end_date, goal, created_at, updated_at)
        VALUES (?, 'S Bad Art', '2026-11-01', '2026-11-10', '', ?, ?)
        """,
        (tid, ts, ts),
    )
    sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO scrum_sprint_item
        (sprint_id, assignee, title, estimate_hours, status, notes, sort_order, created_at, updated_at, kanban_column, task_kind)
        VALUES (?, ?, 'X', 1, 'open', '', 0, ?, ?, 'doing', 'ndy')
        """,
        (sid, EMPLOYEES[0], ts, ts),
    )
    iid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    conn.close()
    r = c.post(
        "/scrum/api/item/move",
        json={
            "item_id": iid,
            "sprint_id": sid,
            "assignee": EMPLOYEES[0],
            "to_column": "done",
            "committed_hours": "0",
            "note": "",
            "artifacts": [{"url": "javascript:alert(1)"}],
        },
    )
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_scrm_api_move_backlog_to_doing_keeps_prior_burnt(client, app):
    c = client
    _manager_login(c)
    conn = get_db(app)
    tid = int(conn.execute("SELECT id FROM teams ORDER BY id ASC LIMIT 1").fetchone()["id"])
    ts = "2026-11-02T00:00:00Z"
    conn.execute(
        """
        INSERT INTO scrum_sprint (team_id, name, start_date, end_date, goal, created_at, updated_at)
        VALUES (?, 'S Burnt', '2026-11-01', '2026-11-10', '', ?, ?)
        """,
        (tid, ts, ts),
    )
    sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO scrum_sprint_item
        (sprint_id, assignee, title, estimate_hours, status, notes, sort_order, created_at, updated_at, kanban_column, task_kind)
        VALUES (?, ?, 'Card2', 1, 'open', '', 0, ?, ?, 'backlog', 'ndy')
        """,
        (sid, EMPLOYEES[0], ts, ts),
    )
    iid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO scrum_item_activity (item_id, body, committed_hours, from_column, to_column, created_at)
        VALUES (?, '', 2.0, 'backlog', 'backlog', ?)
        """,
        (iid, ts),
    )
    conn.commit()
    conn.close()
    r = c.post(
        "/scrum/api/item/move",
        json={
            "item_id": iid,
            "sprint_id": sid,
            "assignee": EMPLOYEES[0],
            "to_column": "doing",
            "committed_hours": "1",
            "note": "move",
        },
    )
    assert r.status_code == 200
    conn = get_db(app)
    total = float(
        conn.execute(
            "SELECT COALESCE(SUM(committed_hours), 0) AS s FROM scrum_item_activity WHERE item_id = ?",
            (iid,),
        ).fetchone()["s"]
    )
    conn.close()
    assert abs(total - 3.0) < 0.001


def test_scrm_api_move_do_to_doing_resets_burnt(client, app):
    c = client
    _manager_login(c)
    conn = get_db(app)
    tid = int(conn.execute("SELECT id FROM teams ORDER BY id ASC LIMIT 1").fetchone()["id"])
    ts = "2026-11-03T00:00:00Z"
    conn.execute(
        """
        INSERT INTO scrum_sprint (team_id, name, start_date, end_date, goal, created_at, updated_at)
        VALUES (?, 'S Reset', '2026-11-01', '2026-11-10', '', ?, ?)
        """,
        (tid, ts, ts),
    )
    sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO scrum_sprint_item
        (sprint_id, assignee, title, estimate_hours, status, notes, sort_order, created_at, updated_at, kanban_column, task_kind)
        VALUES (?, ?, 'Card3', 1, 'open', '', 0, ?, ?, 'do', 'ndy')
        """,
        (sid, EMPLOYEES[0], ts, ts),
    )
    iid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO scrum_item_activity (item_id, body, committed_hours, from_column, to_column, created_at)
        VALUES (?, '', 10.0, 'do', 'do', ?)
        """,
        (iid, ts),
    )
    conn.commit()
    conn.close()
    r = c.post(
        "/scrum/api/item/move",
        json={
            "item_id": iid,
            "sprint_id": sid,
            "assignee": EMPLOYEES[0],
            "to_column": "doing",
            "committed_hours": "0.5",
            "note": "into doing",
        },
    )
    assert r.status_code == 200
    conn = get_db(app)
    nrows = int(
        conn.execute("SELECT COUNT(*) AS c FROM scrum_item_activity WHERE item_id = ?", (iid,)).fetchone()["c"]
    )
    total = float(
        conn.execute(
            "SELECT COALESCE(SUM(committed_hours), 0) AS s FROM scrum_item_activity WHERE item_id = ?",
            (iid,),
        ).fetchone()["s"]
    )
    conn.close()
    assert nrows == 1
    assert abs(total - 0.5) < 0.001


def test_scrm_api_move_doing_to_do_resets_burnt(client, app):
    c = client
    _manager_login(c)
    conn = get_db(app)
    tid = int(conn.execute("SELECT id FROM teams ORDER BY id ASC LIMIT 1").fetchone()["id"])
    ts = "2026-11-03T01:00:00Z"
    conn.execute(
        """
        INSERT INTO scrum_sprint (team_id, name, start_date, end_date, goal, created_at, updated_at)
        VALUES (?, 'S DoingToDo', '2026-11-01', '2026-11-10', '', ?, ?)
        """,
        (tid, ts, ts),
    )
    sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO scrum_sprint_item
        (sprint_id, assignee, title, estimate_hours, status, notes, sort_order, created_at, updated_at, kanban_column, task_kind)
        VALUES (?, ?, 'DoingCard', 8, 'doing', '', 0, ?, ?, 'doing', 'ndy')
        """,
        (sid, EMPLOYEES[0], ts, ts),
    )
    iid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO scrum_item_activity (item_id, body, committed_hours, from_column, to_column, created_at)
        VALUES (?, 'standup', 2.5, 'doing', 'doing', ?)
        """,
        (iid, ts),
    )
    conn.execute(
        """
        INSERT INTO scrum_item_activity (item_id, body, committed_hours, from_column, to_column, created_at)
        VALUES (?, '', 1.0, 'do', 'doing', ?)
        """,
        (iid, ts),
    )
    conn.commit()
    conn.close()
    r = c.post(
        "/scrum/api/item/move",
        json={
            "item_id": iid,
            "sprint_id": sid,
            "assignee": EMPLOYEES[0],
            "to_column": "do",
            "committed_hours": "99",
            "note": "ignored hours",
        },
    )
    assert r.status_code == 200
    conn = get_db(app)
    col = conn.execute("SELECT kanban_column FROM scrum_sprint_item WHERE id = ?", (iid,)).fetchone()["kanban_column"]
    nrows = int(
        conn.execute("SELECT COUNT(*) AS c FROM scrum_item_activity WHERE item_id = ?", (iid,)).fetchone()["c"]
    )
    total = float(
        conn.execute(
            "SELECT COALESCE(SUM(committed_hours), 0) AS s FROM scrum_item_activity WHERE item_id = ?",
            (iid,),
        ).fetchone()["s"]
    )
    last_h = float(
        conn.execute(
            "SELECT committed_hours FROM scrum_item_activity WHERE item_id = ? ORDER BY id DESC LIMIT 1",
            (iid,),
        ).fetchone()["committed_hours"]
        or 0
    )
    conn.close()
    assert col == "do"
    assert nrows == 1
    assert abs(total) < 0.001
    assert abs(last_h) < 0.001


def test_scrm_api_move_do_to_doing_empty_hours_stores_null(client, app):
    c = client
    _manager_login(c)
    conn = get_db(app)
    tid = int(conn.execute("SELECT id FROM teams ORDER BY id ASC LIMIT 1").fetchone()["id"])
    ts = "2026-11-04T00:00:00Z"
    conn.execute(
        """
        INSERT INTO scrum_sprint (team_id, name, start_date, end_date, goal, created_at, updated_at)
        VALUES (?, 'S NullH', '2026-11-01', '2026-11-10', '', ?, ?)
        """,
        (tid, ts, ts),
    )
    sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO scrum_sprint_item
        (sprint_id, assignee, title, estimate_hours, status, notes, sort_order, created_at, updated_at, kanban_column, task_kind)
        VALUES (?, ?, 'CardNull', 2, 'open', '', 0, ?, ?, 'do', 'ndy')
        """,
        (sid, EMPLOYEES[0], ts, ts),
    )
    iid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    conn.close()
    r = c.post(
        "/scrum/api/item/move",
        json={
            "item_id": iid,
            "sprint_id": sid,
            "assignee": EMPLOYEES[0],
            "to_column": "doing",
            "committed_hours": None,
            "note": "",
        },
    )
    assert r.status_code == 200
    conn = get_db(app)
    ch = conn.execute(
        "SELECT committed_hours FROM scrum_item_activity WHERE item_id = ? ORDER BY id DESC LIMIT 1",
        (iid,),
    ).fetchone()["committed_hours"]
    total = float(
        conn.execute(
            "SELECT COALESCE(SUM(committed_hours), 0) AS s FROM scrum_item_activity WHERE item_id = ?",
            (iid,),
        ).fetchone()["s"]
    )
    conn.close()
    assert ch is None
    assert abs(total) < 0.001
    name = EMPLOYEES[0]
    with client.session_transaction() as sess:
        sess["portal_user"] = {
            "email": "portal.test@nokia.com",
            "name": name,
            "roster_name": name,
            "role": "employee",
        }
    conn = get_db(app)
    tid = int(conn.execute("SELECT id FROM teams ORDER BY id ASC LIMIT 1").fetchone()["id"])
    ts = "2026-11-01T00:00:00Z"
    conn.execute(
        """
        INSERT INTO scrum_sprint (team_id, name, start_date, end_date, goal, created_at, updated_at)
        VALUES (?, 'Portal Board Sprint', '2026-11-01', '2026-11-10', '', ?, ?)
        """,
        (tid, ts, ts),
    )
    sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO scrum_sprint_item
        (sprint_id, assignee, title, estimate_hours, status, notes, sort_order, created_at, updated_at, kanban_column, task_kind)
        VALUES (?, ?, 'Portal Kanban Card', 1, 'open', '', 0, ?, ?, 'backlog', 'task')
        """,
        (sid, name, ts, ts),
    )
    conn.commit()
    conn.close()
    board = client.get("/portal/sprint/%d/board" % sid)
    assert board.status_code == 200
    assert b"Backlog" in board.data
    assert b"Portal Kanban Card" in board.data
    assert b"/portal/scrum/api/item/move" in board.data
    assert b"kb-doing-reset-dialog" in board.data
    assert b"Dashboard" in board.data
    hop = client.get("/portal/sprint", follow_redirects=False)
    assert hop.status_code in (302, 303)
    assert "/portal/my-sprint/board" in (hop.headers.get("Location") or "")
    hop2 = client.get("/portal/my-sprint/board", follow_redirects=False)
    assert hop2.status_code in (302, 303)
    assert "/portal/sprint/%d/board" % sid in (hop2.headers.get("Location") or "")


def test_portal_scrum_api_move_queues_proposal_manager_approve_writes_activity(client, app):
    name = EMPLOYEES[0]
    with client.session_transaction() as sess:
        sess["portal_user"] = {
            "email": "portal.test@nokia.com",
            "name": name,
            "roster_name": name,
            "role": "employee",
        }
    conn = get_db(app)
    tid = int(conn.execute("SELECT id FROM teams ORDER BY id ASC LIMIT 1").fetchone()["id"])
    ts = "2026-11-01T00:00:00Z"
    conn.execute(
        """
        INSERT INTO scrum_sprint (team_id, name, start_date, end_date, goal, created_at, updated_at)
        VALUES (?, 'Portal Move Sprint', '2026-11-01', '2026-11-10', '', ?, ?)
        """,
        (tid, ts, ts),
    )
    sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO scrum_sprint_item
        (sprint_id, assignee, title, estimate_hours, status, notes, sort_order, created_at, updated_at, kanban_column, task_kind)
        VALUES (?, ?, 'Move me', 1, 'open', '', 0, ?, ?, 'backlog', 'task')
        """,
        (sid, name, ts, ts),
    )
    iid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    conn.close()
    r = client.post(
        "/portal/scrum/api/item/move",
        json={
            "item_id": iid,
            "sprint_id": sid,
            "assignee": name,
            "to_column": "doing",
            "committed_hours": "0.25",
            "note": "picked up",
        },
    )
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert j.get("pending_approval") is True
    conn = get_db(app)
    n_act = int(
        conn.execute("SELECT COUNT(*) AS c FROM scrum_item_activity WHERE item_id = ?", (iid,)).fetchone()["c"]
    )
    assert n_act == 0
    col = conn.execute("SELECT kanban_column FROM scrum_sprint_item WHERE id = ?", (iid,)).fetchone()["kanban_column"]
    assert col == "backlog"
    pid = int(
        conn.execute(
            "SELECT id FROM scrum_portal_proposal WHERE item_id = ? AND status = 'pending'",
            (iid,),
        ).fetchone()["id"]
    )
    conn.close()

    _manager_login(client)
    res = client.post(
        "/scrum/portal-proposal/resolve",
        data={"proposal_id": str(pid), "decision": "approve", "note": "ok", "manager_attest": "1"},
        follow_redirects=False,
    )
    assert res.status_code in (302, 303)

    conn = get_db(app)
    body = conn.execute(
        "SELECT body FROM scrum_item_activity WHERE item_id = ? ORDER BY id DESC LIMIT 1",
        (iid,),
    ).fetchone()["body"]
    col2 = conn.execute("SELECT kanban_column FROM scrum_sprint_item WHERE id = ?", (iid,)).fetchone()["kanban_column"]
    conn.close()
    assert "[approved employee change]" in body
    assert "picked up" in body
    assert col2 == "doing"


def test_portal_proposal_approve_without_attest_does_not_apply(client, app):
    name = EMPLOYEES[0]
    with client.session_transaction() as sess:
        sess["portal_user"] = {
            "email": "portal.test@nokia.com",
            "name": name,
            "roster_name": name,
            "role": "employee",
        }
    conn = get_db(app)
    tid = int(conn.execute("SELECT id FROM teams ORDER BY id ASC LIMIT 1").fetchone()["id"])
    ts = "2026-11-01T00:00:00Z"
    conn.execute(
        """
        INSERT INTO scrum_sprint (team_id, name, start_date, end_date, goal, created_at, updated_at)
        VALUES (?, 'Attest Sprint', '2026-11-01', '2026-11-10', '', ?, ?)
        """,
        (tid, ts, ts),
    )
    sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO scrum_sprint_item
        (sprint_id, assignee, title, estimate_hours, status, notes, sort_order, created_at, updated_at, kanban_column, task_kind)
        VALUES (?, ?, 'Attest card', 1, 'open', '', 0, ?, ?, 'backlog', 'task')
        """,
        (sid, name, ts, ts),
    )
    iid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    conn.close()
    assert client.post(
        "/portal/scrum/api/item/move",
        json={
            "item_id": iid,
            "sprint_id": sid,
            "assignee": name,
            "to_column": "doing",
            "committed_hours": "0",
            "note": "x",
        },
    ).get_json()["ok"]
    conn = get_db(app)
    pid = int(
        conn.execute(
            "SELECT id FROM scrum_portal_proposal WHERE item_id = ? AND status = 'pending'",
            (iid,),
        ).fetchone()["id"]
    )
    conn.close()
    _manager_login(client)
    client.post(
        "/scrum/portal-proposal/resolve",
        data={"proposal_id": str(pid), "decision": "approve", "note": "no checkbox"},
        follow_redirects=False,
    )
    conn = get_db(app)
    col = conn.execute("SELECT kanban_column FROM scrum_sprint_item WHERE id = ?", (iid,)).fetchone()["kanban_column"]
    st = conn.execute("SELECT status FROM scrum_portal_proposal WHERE id = ?", (pid,)).fetchone()["status"]
    conn.close()
    assert col == "backlog"
    assert st == "pending"


def test_attendance_url_redirects_home(client):
    assert client.get("/attendance", follow_redirects=False).status_code == 302


def test_legacy_manager_paths_redirect(client):
    c = client
    assert c.get("/manager", follow_redirects=False).status_code == 302
    assert c.get("/manager/login", follow_redirects=False).status_code == 302
    _manager_login(c)
    assert c.get("/manager?year=2026&month=5", follow_redirects=False).status_code == 302
