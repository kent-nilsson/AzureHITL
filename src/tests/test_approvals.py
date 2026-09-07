"""Tests for the HITL approval workflow: tokens, approver resolution, decisions."""

from __future__ import annotations

import time

import pytest

from app import approvals
from app.approvals import (
    AlreadyDecidedError,
    ApprovalTokenError,
    apply_decision,
    resolve_approver,
    sign,
    verify,
)
from app.models import GuideRecord, GuideStatus, StudyGuide, StudyWeek
from app.storage import get_repo


def _make_guide(learner="learner@example.com") -> GuideRecord:
    rec = GuideRecord(
        session_id="sess_x",
        learner_email=learner,
        learner_name="Test Learner",
        approver_email=resolve_approver(learner),
        guide=StudyGuide(
            target_certification="AZ-204",
            weeks=[StudyWeek(week=1, focus="Functions", activities=["study"])],
        ),
    )
    get_repo().put_guide(rec)
    return rec


# --- tokens ----------------------------------------------------------------
def test_sign_verify_roundtrip():
    token = sign("guide_123", "approve")
    assert verify(token) == ("guide_123", "approve")


def test_tampered_token_rejected():
    token = sign("guide_123", "approve")
    body, sig = token.split(".")
    with pytest.raises(ApprovalTokenError):
        verify(f"{body}.{sig[:-2]}xx")


def test_expired_token_rejected():
    token = sign("guide_123", "reject", expires_at=int(time.time()) - 1)
    with pytest.raises(ApprovalTokenError):
        verify(token)


def test_garbage_token_rejected():
    with pytest.raises(ApprovalTokenError):
        verify("not-a-real-token")


# --- approver resolution -------------------------------------------------
def test_resolve_approver_uses_mapping_then_default():
    assert resolve_approver("vip@example.com") == "cto@example.com"
    assert resolve_approver("VIP@example.com") == "cto@example.com"
    assert resolve_approver("nobody@example.com") == "boss@example.com"


# --- decisions ---------------------------------------------------------
def test_apply_decision_approves_and_notifies(monkeypatch):
    sent: list[GuideRecord] = []
    monkeypatch.setattr(approvals, "notify_learner", lambda rec: sent.append(rec))

    rec = _make_guide()
    updated = apply_decision(sign(rec.guide_id, "approve"))

    assert updated.status == GuideStatus.APPROVED
    assert updated.decided_at is not None
    assert get_repo().get_guide(rec.guide_id).status == GuideStatus.APPROVED
    assert sent and sent[0].guide_id == rec.guide_id


def test_apply_decision_rejects_with_comment(monkeypatch):
    monkeypatch.setattr(approvals, "notify_learner", lambda rec: None)
    rec = _make_guide()
    updated = apply_decision(sign(rec.guide_id, "reject"), comment="Too aggressive")
    assert updated.status == GuideStatus.REJECTED
    assert updated.approver_comment == "Too aggressive"


def test_links_are_single_use(monkeypatch):
    monkeypatch.setattr(approvals, "notify_learner", lambda rec: None)
    rec = _make_guide()

    apply_decision(sign(rec.guide_id, "approve"))
    with pytest.raises(AlreadyDecidedError):
        apply_decision(sign(rec.guide_id, "reject"))


def test_request_approval_console_email(caplog):
    rec = _make_guide()
    with caplog.at_level("INFO", logger="app.approvals"):
        approvals.request_approval(rec)

    logged = "\n".join(caplog.messages)
    assert "EMAIL (console)" in logged
    assert rec.approver_email in logged
    assert "APPROVE:" in logged and "REJECT:" in logged


def test_decision_links_shape():
    rec = _make_guide()
    links = approvals.decision_links(rec.guide_id)
    assert links["approve"].startswith("http://testserver/api/decision?token=")
    assert links["reject"] != links["approve"]
    assert verify(_token_from(links["approve"])) == (rec.guide_id, "approve")


def _token_from(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(url).query)["token"][0]
