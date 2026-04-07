import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlite3
from app import ApprovalService


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


def test_three_step_approval_salesforce_event(tmp_path: Path):
    db_path = tmp_path / "test.db"
    service = ApprovalService(str(db_path))

    rid = service.create_request(
        requester="alice@company.com",
        request_type="Discount Quote",
        title="Deal Discount",
        details="Need approval",
        first_approver="manager1@company.com",
        external_system="salesforce",
        external_reference="OPP-7788",
    )

    assert service.decide(rid, "manager1@company.com", "approve", "director@company.com") == "Step 1 approved."
    assert service.decide(rid, "director@company.com", "approve", "hr@company.com") == "Step 2 approved."
    assert service.decide(rid, "hr@company.com", "approve", "") == "Final approval complete."

    req, _ = service.get_request(rid)
    assert req["status"] == "approved"

    with sqlite3.connect(db_path) as db:
        row = db.execute("SELECT system_name, external_id, status_payload FROM integration_events").fetchone()
    assert row == ("salesforce", "OPP-7788", "Approved")
