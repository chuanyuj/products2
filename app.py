from __future__ import annotations

import html
import json
import os
import secrets
import sqlite3
from http import cookies
from pathlib import Path
from urllib import error, request as urlrequest
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "approval.db"
DEFAULT_SF_TOKEN = "salesforce-demo-token"


class ApprovalService:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()
        self._seed_users()

    def _connect(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def _init_db(self):
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  email TEXT UNIQUE NOT NULL,
                  password TEXT NOT NULL,
                  display_name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approval_requests (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  requester_email TEXT NOT NULL,
                  request_type TEXT NOT NULL,
                  title TEXT NOT NULL,
                  details TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'in_progress',
                  current_step INTEGER NOT NULL DEFAULT 1,
                  external_system TEXT,
                  external_reference TEXT,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS approval_steps (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  request_id INTEGER NOT NULL,
                  step_number INTEGER NOT NULL,
                  approver_email TEXT,
                  status TEXT NOT NULL,
                  comments TEXT
                );
                CREATE TABLE IF NOT EXISTS integration_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  system_name TEXT NOT NULL,
                  external_id TEXT NOT NULL,
                  status_payload TEXT NOT NULL,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def _seed_users(self):
        seed = [
            ("alice@company.com", "alice123", "Alice"),
            ("manager1@company.com", "manager123", "Manager 1"),
            ("director@company.com", "director123", "Director"),
            ("hr@company.com", "hr123", "HR"),
        ]
        with self._connect() as db:
            for u in seed:
                db.execute("INSERT OR IGNORE INTO users (email,password,display_name) VALUES (?,?,?)", u)

    def login(self, email: str, password: str) -> bool:
        with self._connect() as db:
            u = db.execute("SELECT 1 FROM users WHERE email=? AND password=?", (email.lower(), password)).fetchone()
            return bool(u)

    def create_request(self, requester, request_type, title, details, first_approver, external_system="", external_reference=""):
        with self._connect() as db:
            c = db.execute(
                "INSERT INTO approval_requests (requester_email,request_type,title,details,external_system,external_reference) VALUES (?,?,?,?,?,?)",
                (requester, request_type, title, details, external_system or None, external_reference or None),
            )
            rid = c.lastrowid
            for step in (1, 2, 3):
                db.execute(
                    "INSERT INTO approval_steps (request_id,step_number,approver_email,status) VALUES (?,?,?,?)",
                    (rid, step, first_approver if step == 1 else None, "pending" if step == 1 else "waiting_assignment"),
                )
            return rid

    def create_salesforce_request(self, requester, title, details, first_approver, opportunity_id):
        rid = self.create_request(
            requester=requester,
            request_type="Discount Quote",
            title=title,
            details=details,
            first_approver=first_approver,
            external_system="salesforce",
            external_reference=opportunity_id,
        )
        with self._connect() as db:
            self._record_salesforce_event(db, opportunity_id, "Submitted")
        return rid

    def list_my_requests(self, user):
        with self._connect() as db:
            return db.execute("SELECT * FROM approval_requests WHERE requester_email=? ORDER BY id DESC", (user,)).fetchall()

    def list_pending(self, user):
        with self._connect() as db:
            return db.execute(
                """
                SELECT r.* FROM approval_requests r
                JOIN approval_steps s ON s.request_id=r.id
                WHERE r.status='in_progress' AND r.current_step=s.step_number
                AND s.status='pending' AND s.approver_email=?
                ORDER BY r.id DESC
                """,
                (user,),
            ).fetchall()

    def get_request(self, rid):
        with self._connect() as db:
            req = db.execute("SELECT * FROM approval_requests WHERE id=?", (rid,)).fetchone()
            steps = db.execute("SELECT * FROM approval_steps WHERE request_id=? ORDER BY step_number", (rid,)).fetchall()
            return req, steps

    def decide(self, rid, actor, decision, next_approver, comments=""):
        with self._connect() as db:
            req = db.execute("SELECT * FROM approval_requests WHERE id=?", (rid,)).fetchone()
            if not req or req["status"] != "in_progress":
                return "Only active requests can be updated."
            step = db.execute("SELECT * FROM approval_steps WHERE request_id=? AND step_number=?", (rid, req["current_step"])).fetchone()
            if step["approver_email"] != actor:
                return "You are not assigned to this step."
            if decision == "reject":
                if not comments.strip():
                    return "Comments are required when rejecting."
                db.execute("UPDATE approval_steps SET status='rejected', comments=? WHERE id=?", (comments, step["id"]))
                db.execute("UPDATE approval_requests SET status='rejected' WHERE id=?", (rid,))
                if req["external_system"] == "salesforce" and req["external_reference"]:
                    self._record_salesforce_event(db, req["external_reference"], f"Manager Rejected: {comments.strip()}")
                return "Request rejected."

            db.execute("UPDATE approval_steps SET status='approved', comments=? WHERE id=?", (comments, step["id"]))
            if req["current_step"] == 3:
                db.execute("UPDATE approval_requests SET status='approved' WHERE id=?", (rid,))
                if req["external_system"] == "salesforce" and req["external_reference"]:
                    self._record_salesforce_event(db, req["external_reference"], "Approved")
                return "Final approval complete."

            if not next_approver:
                return "You must assign the next approver."
            next_step = req["current_step"] + 1
            db.execute(
                "UPDATE approval_steps SET approver_email=?, status='pending' WHERE request_id=? AND step_number=?",
                (next_approver, rid, next_step),
            )
            db.execute("UPDATE approval_requests SET current_step=? WHERE id=?", (next_step, rid))
            if req["external_system"] == "salesforce" and req["external_reference"]:
                status_payload = f"Step {next_step}"
                if req["current_step"] == 1:
                    status_payload = "Manager Approved"
                self._record_salesforce_event(db, req["external_reference"], status_payload)
            return f"Step {req['current_step']} approved."

    def _record_salesforce_event(self, db: sqlite3.Connection, opportunity_id: str, status_payload: str) -> None:
        db.execute(
            "INSERT INTO integration_events (system_name,external_id,status_payload) VALUES ('salesforce',?, ?)",
            (opportunity_id, status_payload),
        )
        self._update_salesforce_opportunity_status(opportunity_id, status_payload)

    def _update_salesforce_opportunity_status(self, opportunity_id: str, status_payload: str) -> bool:
        instance_url = os.getenv("SF_INSTANCE_URL", "").rstrip("/")
        access_token = os.getenv("SF_ACCESS_TOKEN", "")
        field_api_name = os.getenv("SF_OPPORTUNITY_STATUS_FIELD", "Approval_Status__c")
        api_version = os.getenv("SF_API_VERSION", "v60.0")

        if not instance_url or not access_token:
            return False

        endpoint = f"{instance_url}/services/data/{api_version}/sobjects/Opportunity/{opportunity_id}"
        payload = json.dumps({field_api_name: status_payload}).encode()
        req = urlrequest.Request(
            endpoint,
            data=payload,
            method="PATCH",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlrequest.urlopen(req, timeout=8):
                return True
        except (error.URLError, error.HTTPError):
            return False


class ApprovalApp:
    def __init__(self, db_path: str = str(DEFAULT_DB_PATH)):
        self.service = ApprovalService(db_path)
        self.sessions: dict[str, str] = {}
        self.salesforce_token = os.getenv("SALESFORCE_API_TOKEN", DEFAULT_SF_TOKEN)

    @staticmethod
    def _read_form_or_json(environ):
        size = int(environ.get("CONTENT_LENGTH") or 0)
        raw = environ["wsgi.input"].read(size) if size > 0 else b""
        content_type = environ.get("CONTENT_TYPE", "")
        if "application/json" in content_type:
            data = json.loads(raw.decode() or "{}")
            return {k: [str(v)] for k, v in data.items()}
        return parse_qs(raw.decode())

    @staticmethod
    def _json(start_response, status_code: str, payload: dict):
        start_response(status_code, [("Content-Type", "application/json")])
        return [json.dumps(payload).encode()]

    def _layout(self, body: str, user: str | None = None, msg: str = ""):
        nav = ""
        if user:
            nav = f"<div><a href='/dashboard'>Dashboard</a> | <a href='/request/new'>New Request</a> | <a href='/logout'>Logout</a></div>"
        flash = f"<p style='color:#0b5'>{html.escape(msg)}</p>" if msg else ""
        return f"""
        <html><head><title>ApprovalFlow</title>
        <style>body{{font-family:Arial;background:#f4f6fb;margin:0}}header{{background:#0f172a;color:#fff;padding:12px 18px;display:flex;justify-content:space-between}}main{{max-width:920px;margin:20px auto;background:#fff;padding:20px;border-radius:10px;box-shadow:0 8px 20px rgba(0,0,0,.08)}}input,select,textarea,button{{width:100%;padding:8px;margin:5px 0}}button{{background:#0b63f6;color:#fff;border:none}}a{{color:#0b63f6}}</style>
        </head><body><header><b>ApprovalFlow</b>{nav}</header><main>{flash}{body}</main></body></html>
        """

    def _get_user(self, environ):
        cookie = cookies.SimpleCookie(environ.get("HTTP_COOKIE", ""))
        sid = cookie.get("sid")
        return self.sessions.get(sid.value) if sid else None

    def _redirect(self, start_response, location, sid=None):
        headers = [("Location", location)]
        if sid:
            headers.append(("Set-Cookie", f"sid={sid}; Path=/"))
        start_response("302 Found", headers)
        return [b""]

    def __call__(self, environ, start_response):
        method = environ["REQUEST_METHOD"]
        path = environ.get("PATH_INFO", "/")
        user = self._get_user(environ)

        if path == "/":
            return self._redirect(start_response, "/dashboard" if user else "/login")

        if path == "/login" and method == "GET":
            form = """
            <h1>Sign in</h1><form method='post'>
            <label>Email</label><input name='email' type='email' required>
            <label>Password</label><input name='password' type='password' required>
            <button>Log in</button></form>
            <small>alice@company.com / alice123</small>
            """
            start_response("200 OK", [("Content-Type", "text/html")])
            return [self._layout(form).encode()]

        if path == "/login" and method == "POST":
            data = self._read_form_or_json(environ)
            email = data.get("email", [""])[0].strip().lower()
            pwd = data.get("password", [""])[0]
            if self.service.login(email, pwd):
                sid = secrets.token_hex(16)
                self.sessions[sid] = email
                return self._redirect(start_response, "/dashboard", sid=sid)
            start_response("200 OK", [("Content-Type", "text/html")])
            return [self._layout("<h1>Sign in</h1><p>Invalid email or password.</p>").encode()]

        if path == "/api/integrations/salesforce/opportunity-submit" and method == "POST":
            data = self._read_form_or_json(environ)
            token = data.get("token", [""])[0]
            if token != self.salesforce_token:
                return self._json(start_response, "401 Unauthorized", {"error": "Unauthorized"})

            requester = data.get("requester_email", [""])[0].strip().lower()
            opportunity_id = data.get("opportunity_id", [""])[0].strip()
            title = data.get("title", [""])[0].strip() or f"Approval for Opportunity {opportunity_id}"
            details = data.get("details", [""])[0].strip() or "Submitted from Salesforce"
            first_approver = data.get("first_approver", [""])[0].strip().lower()

            if not requester or not opportunity_id or not first_approver:
                return self._json(
                    start_response,
                    "400 Bad Request",
                    {"error": "requester_email, opportunity_id and first_approver are required"},
                )

            rid = self.service.create_salesforce_request(
                requester=requester,
                title=title,
                details=details,
                first_approver=first_approver,
                opportunity_id=opportunity_id,
            )
            return self._json(
                start_response,
                "201 Created",
                {"request_id": rid, "opportunity_id": opportunity_id, "status": "submitted"},
            )

        if path == "/logout":
            return self._redirect(start_response, "/login")

        if not user:
            return self._redirect(start_response, "/login")

        if path == "/dashboard":
            my = self.service.list_my_requests(user)
            pending = self.service.list_pending(user)
            rows_my = "".join([f"<li><a href='/request/{r['id']}'>#{r['id']} {html.escape(r['title'])}</a> ({r['status']})</li>" for r in my]) or "<li>No requests</li>"
            rows_pending = "".join([f"<li><a href='/request/{r['id']}'>#{r['id']} {html.escape(r['title'])}</a> (step {r['current_step']})</li>" for r in pending]) or "<li>No pending approvals</li>"
            body = f"<h1>Approval Dashboard</h1><h3>Pending my approvals</h3><ul>{rows_pending}</ul><h3>My submitted requests</h3><ul>{rows_my}</ul>"
            start_response("200 OK", [("Content-Type", "text/html")])
            return [self._layout(body, user).encode()]

        if path == "/request/new" and method == "GET":
            body = """
            <h1>Start 3-step approval</h1>
            <form method='post'>
            <label>Request type</label><select name='request_type'><option>Day Off</option><option>Discount Quote</option><option>Purchase</option></select>
            <label>Title</label><input name='title' required>
            <label>Details</label><textarea name='details' required></textarea>
            <label>First approver email</label><input name='first_approver' type='email' required>
            <h3>Optional Salesforce integration</h3>
            <label>External system</label><select name='external_system'><option value=''>None</option><option value='salesforce'>Salesforce</option></select>
            <label>External reference (Opportunity ID)</label><input name='external_reference'>
            <button>Submit request</button></form>
            """
            start_response("200 OK", [("Content-Type", "text/html")])
            return [self._layout(body, user).encode()]

        if path == "/request/new" and method == "POST":
            data = self._read_form_or_json(environ)
            rid = self.service.create_request(
                user,
                data.get("request_type", [""])[0],
                data.get("title", [""])[0],
                data.get("details", [""])[0],
                data.get("first_approver", [""])[0].lower(),
                data.get("external_system", [""])[0],
                data.get("external_reference", [""])[0],
            )
            return self._redirect(start_response, f"/request/{rid}")

        if path.startswith("/request/") and method == "GET":
            rid = int(path.split("/")[-1])
            req, steps = self.service.get_request(rid)
            if not req:
                start_response("404 Not Found", [("Content-Type", "text/plain")])
                return [b"Not Found"]
            steps_html = "".join(
                [
                    f"<li>Step {s['step_number']}: {s['status']} {html.escape(s['approver_email'] or '')}"
                    f"{' | Comment: ' + html.escape(s['comments']) if s['comments'] else ''}</li>"
                    for s in steps
                ]
            )
            body = f"""
            <h1>Request #{req['id']} - {html.escape(req['title'])}</h1>
            <p><b>Type:</b> {html.escape(req['request_type'])}</p>
            <p><b>Status:</b> {req['status']}</p>
            <p><b>Current Step:</b> {req['current_step']}</p>
            <p>{html.escape(req['details'])}</p>
            <ol>{steps_html}</ol>
            <form method='post' action='/request/{rid}/decision'>
            <label>Decision</label><select name='decision'><option value='approve'>Approve</option><option value='reject'>Reject</option></select>
            <label>Next approver (required for step 1 and 2 approval)</label><input name='next_approver' type='email'>
            <label>Comment</label><textarea name='comments'></textarea>
            <button>Submit decision</button></form>
            """
            start_response("200 OK", [("Content-Type", "text/html")])
            return [self._layout(body, user).encode()]

        if path.startswith("/request/") and path.endswith("/decision") and method == "POST":
            rid = int(path.split("/")[2])
            data = self._read_form_or_json(environ)
            msg = self.service.decide(
                rid,
                user,
                data.get("decision", [""])[0],
                data.get("next_approver", [""])[0].lower(),
                data.get("comments", [""])[0],
            )
            req, steps = self.service.get_request(rid)
            steps_html = "".join(
                [
                    f"<li>Step {s['step_number']}: {s['status']} {html.escape(s['approver_email'] or '')}"
                    f"{' | Comment: ' + html.escape(s['comments']) if s['comments'] else ''}</li>"
                    for s in steps
                ]
            )
            body = f"<h1>Request #{rid}</h1><p>Status: {req['status']}</p><ol>{steps_html}</ol><a href='/request/{rid}'>Reload detail</a>"
            start_response("200 OK", [("Content-Type", "text/html")])
            return [self._layout(body, user, msg).encode()]

        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"Not Found"]


def create_app(db_path: str = str(DEFAULT_DB_PATH)):
    return ApprovalApp(db_path)


if __name__ == "__main__":
    app = create_app()
    print("Running on http://127.0.0.1:8000")
    make_server("127.0.0.1", 8000, app).serve_forever()
