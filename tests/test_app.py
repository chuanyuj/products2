import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import io
import json
import sqlite3
from app import ApprovalApp, ApprovalService


def test_login_and_create_request(tmp_path: Path):
    db_path = tmp_path / "test.db"
    service = ApprovalService(str(db_path))

    assert service.login("alice@company.com", "alice123")
    rid = service.create_request(
        requester="alice@company.com",
        request_type="Day Off",
        title="Vacation",
        details="Need Friday off",
        first_approver="manager1@company.com",
    )
    assert rid == 1

    my_requests = service.list_my_requests("alice@company.com")
    assert len(my_requests) == 1
    assert my_requests[0]["title"] == "Vacation"


def test_three_step_approval_salesforce_event(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "test.db"
    service = ApprovalService(str(db_path))
    pushed_updates = []

    def fake_push(opportunity_id, status_payload):
        pushed_updates.append((opportunity_id, status_payload))
        return True

    monkeypatch.setattr(service, "_update_salesforce_opportunity_status", fake_push)

    rid = service.create_salesforce_request(
        requester="alice@company.com",
        title="Deal Discount",
        details="Need approval",
        first_approver="manager1@company.com",
        opportunity_id="OPP-7788",
    )

    assert service.decide(rid, "manager1@company.com", "approve", "director@company.com") == "Step 1 approved."
    assert service.decide(rid, "director@company.com", "approve", "hr@company.com") == "Step 2 approved."
    assert service.decide(rid, "hr@company.com", "approve", "") == "Final approval complete."

    req, _ = service.get_request(rid)
    assert req["status"] == "approved"

    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            "SELECT system_name, external_id, status_payload FROM integration_events ORDER BY id"
        ).fetchall()
    assert rows == [
        ("salesforce", "OPP-7788", "Submitted"),
        ("salesforce", "OPP-7788", "Manager Approved"),
        ("salesforce", "OPP-7788", "Step 3"),
        ("salesforce", "OPP-7788", "Approved"),
    ]
    assert pushed_updates == [
        ("OPP-7788", "Submitted"),
        ("OPP-7788", "Manager Approved"),
        ("OPP-7788", "Step 3"),
        ("OPP-7788", "Approved"),
    ]


def test_reject_requires_comment_and_records_rejection(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "test.db"
    service = ApprovalService(str(db_path))
    monkeypatch.setattr(service, "_update_salesforce_opportunity_status", lambda *_: True)

    rid = service.create_request(
        requester="alice@company.com",
        request_type="Discount Quote",
        title="Deal Discount",
        details="Need approval",
        first_approver="manager1@company.com",
        external_system="salesforce",
        external_reference="OPP-9001",
    )

    assert service.decide(rid, "manager1@company.com", "reject", "", comments="") == "Comments are required when rejecting."
    assert service.decide(rid, "manager1@company.com", "reject", "", comments="Discount too high") == "Request rejected."

    req, steps = service.get_request(rid)
    assert req["status"] == "rejected"
    assert steps[0]["comments"] == "Discount too high"

    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT system_name, external_id, status_payload FROM integration_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row == ("salesforce", "OPP-9001", "Manager Rejected: Discount too high")


def test_salesforce_submit_endpoint_creates_request(tmp_path: Path):
    app = ApprovalApp(str(tmp_path / "test.db"))

    payload = json.dumps(
        {
            "token": "salesforce-demo-token",
            "requester_email": "alice@company.com",
            "opportunity_id": "OPP-3000",
            "title": "Submit from Salesforce",
            "details": "Discount needs manager approval",
            "first_approver": "manager1@company.com",
        }
    ).encode()

    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/api/integrations/salesforce/opportunity-submit",
        "CONTENT_TYPE": "application/json",
        "CONTENT_LENGTH": str(len(payload)),
        "wsgi.input": io.BytesIO(payload),
    }

    status_headers = {}

    def start_response(status, headers):
        status_headers["status"] = status
        status_headers["headers"] = headers

    body = b"".join(app(environ, start_response))
    assert status_headers["status"] == "201 Created"
    response = json.loads(body.decode())
    assert response["status"] == "submitted"
    assert response["opportunity_id"] == "OPP-3000"

    req, _ = app.service.get_request(response["request_id"])
    assert req["external_system"] == "salesforce"
    assert req["external_reference"] == "OPP-3000"
