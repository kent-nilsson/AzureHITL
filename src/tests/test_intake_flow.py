"""End-to-end intake → generate → approval flow through the FastAPI app."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import approvals
from app.models import StudyGuide, StudyResource, StudyWeek


@pytest.fixture
def canned_guide(monkeypatch):
    guide = StudyGuide(
        target_certification="Microsoft Certified: Azure Developer Associate",
        certification_url="https://learn.microsoft.com/credentials/certifications/azure-developer/",
        exam_codes=["AZ-204"],
        rationale="Matches the learner's Python background and stated goal.",
        weekly_hours=6,
        weeks=[
            StudyWeek(
                week=1,
                focus="Azure Functions",
                activities=["Complete the Functions learning path"],
                resources=[
                    StudyResource(
                        title="Implement Azure Functions",
                        url="https://learn.microsoft.com/training/paths/implement-azure-functions/",
                        kind="learningPath",
                        uid="learn.az204-functions",
                        duration_minutes=180,
                    )
                ],
            )
        ],
    )
    monkeypatch.setattr("app.main.run_study_guide", lambda answers: guide)
    return guide


@pytest.fixture
def sent_emails(monkeypatch):
    approval: list = []
    learner: list = []
    monkeypatch.setattr("app.main.request_approval", approval.append)
    monkeypatch.setattr(approvals, "notify_learner", learner.append)
    return {"approval": approval, "learner": learner}


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


def _answer(client, session_id, text):
    r = client.post("/api/message", json={"session_id": session_id, "text": text})
    assert r.status_code == 200, r.text
    return r.json()


def test_three_questions_asked_one_at_a_time(client):
    state = client.post("/api/session", json={}).json()
    assert state["step"] == "ASK_CERTS"
    assert "certifications do you currently hold" in state["prompt"]

    sid = state["session_id"]
    state = _answer(client, sid, "AZ-900")
    assert state["step"] == "ASK_BACKGROUND"
    assert "technical background" in state["prompt"]
    assert state["answers"]["certificates"] == "AZ-900"

    state = _answer(client, sid, "3 years as a Python developer")
    assert state["step"] == "ASK_GOAL"
    assert state["answers"]["background"] == "3 years as a Python developer"

    # a resumed session carries the answered Q&A so the transcript reads cleanly
    resumed = client.get(f"/api/session/{sid}").json()
    texts = [t["text"] for t in resumed["history"]]
    assert texts[1] == "AZ-900" and texts[3] == "3 years as a Python developer"
    assert all(t["role"] in ("bot", "user") for t in resumed["history"])


def test_full_flow_to_pending_then_approved(client, canned_guide, sent_emails):
    sid = client.post("/api/session", json={}).json()["session_id"]
    _answer(client, sid, "AZ-900")
    _answer(client, sid, "3 years as a Python developer")
    state = _answer(client, sid, "Become an Azure Developer Associate")

    assert state["step"] == "PENDING_APPROVAL"
    assert state["guide"]["status"] == "PendingApproval"
    assert state["guide"]["approver_email"] == "boss@example.com"
    assert "Azure Developer" in state["guide"]["markdown"]
    assert len(sent_emails["approval"]) == 1

    guide_id = state["guide"]["guide_id"]
    token = approvals.sign(guide_id, "approve")

    # GET is a safe confirmation page — it must NOT decide anything.
    r = client.get("/api/decision", params={"token": token})
    assert r.status_code == 200
    assert "confirm" in r.text.lower()
    assert client.get(f"/api/guides/{guide_id}").json()["status"] == "PendingApproval"
    assert sent_emails["learner"] == []

    # The POST commits the decision.
    r = client.post("/api/decision", data={"token": token, "comment": "Looks good"})
    assert r.status_code == 200
    assert "approved" in r.text.lower()

    g = client.get(f"/api/guides/{guide_id}").json()
    assert g["status"] == "Approved"
    assert g["approver_comment"] == "Looks good"
    assert len(sent_emails["learner"]) == 1

    # session reflects the decision
    s = client.get(f"/api/session/{sid}").json()
    assert s["step"] == "PENDING_APPROVAL"  # session step unchanged; guide drives UI
    assert s["guide"]["status"] == "Approved"


def test_reused_link_returns_conflict(client, canned_guide, sent_emails):
    sid = client.post("/api/session", json={}).json()["session_id"]
    _answer(client, sid, "none")
    _answer(client, sid, "new to Azure")
    state = _answer(client, sid, "AZ-900 fundamentals")
    guide_id = state["guide"]["guide_id"]

    client.post("/api/decision", data={"token": approvals.sign(guide_id, "approve")})
    # both the confirm page and a second commit must refuse
    r = client.get("/api/decision", params={"token": approvals.sign(guide_id, "reject")})
    assert r.status_code == 409
    assert "single-use" in r.text.lower()
    r = client.post("/api/decision", data={"token": approvals.sign(guide_id, "reject")})
    assert r.status_code == 409


def test_message_after_intake_complete_is_rejected(client, canned_guide, sent_emails):
    sid = client.post("/api/session", json={}).json()["session_id"]
    _answer(client, sid, "none")
    _answer(client, sid, "new to Azure")
    _answer(client, sid, "AZ-900")

    r = client.post("/api/message", json={"session_id": sid, "text": "hello?"})
    assert r.status_code == 409
