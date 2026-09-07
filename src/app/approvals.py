"""Human-in-the-loop approval workflow.

The learner never approves their own plan. Instead the app:
  1. resolves the learner's preassigned boss (``resolve_approver``),
  2. e-mails that boss an Approve and a Reject link, each carrying an
     HMAC-signed, time-limited, single-use token,
  3. on click, verifies the token and records the decision (``apply_decision``),
  4. e-mails the learner the outcome.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from urllib.parse import urlencode

from .config import get_settings
from .models import GuideRecord, GuideStatus

logger = logging.getLogger(__name__)

_VALID_ACTIONS = ("approve", "reject")


class ApprovalError(Exception):
    """Base class for approval-flow failures."""


class ApprovalTokenError(ApprovalError):
    """Token missing, malformed, tampered, or expired."""


class AlreadyDecidedError(ApprovalError):
    """The guide already has a decision (single-use enforcement)."""

    def __init__(self, guide: GuideRecord) -> None:
        super().__init__(f"guide {guide.guide_id} is already {guide.status.value}")
        self.guide = guide


# ---------------------------------------------------------------------------
# Token signing
# ---------------------------------------------------------------------------
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sig(payload: str) -> str:
    key = get_settings().approval_signing_key.encode("utf-8")
    return _b64e(hmac.new(key, payload.encode("utf-8"), hashlib.sha256).digest())


def sign(guide_id: str, action: str, expires_at: int | None = None) -> str:
    if action not in _VALID_ACTIONS:
        raise ValueError(f"action must be one of {_VALID_ACTIONS}")
    if expires_at is None:
        ttl = get_settings().approval_link_ttl_hours * 3600
        expires_at = int(time.time()) + ttl
    payload = f"{guide_id}:{action}:{expires_at}"
    return f"{_b64e(payload.encode('utf-8'))}.{_sig(payload)}"


def verify(token: str) -> tuple[str, str]:
    """Return ``(guide_id, action)`` for a valid token, else raise."""

    try:
        payload_b64, provided_sig = token.split(".", 1)
        payload = _b64d(payload_b64).decode("utf-8")
        guide_id, action, expires_at = payload.split(":")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApprovalTokenError("malformed token") from exc

    if not hmac.compare_digest(provided_sig, _sig(payload)):
        raise ApprovalTokenError("bad signature")
    if action not in _VALID_ACTIONS:
        raise ApprovalTokenError("unknown action")
    if int(expires_at) < int(time.time()):
        raise ApprovalTokenError("token expired")
    return guide_id, action


# ---------------------------------------------------------------------------
# Approver resolution
# ---------------------------------------------------------------------------
def resolve_approver(learner_email: str) -> str:
    settings = get_settings()
    return settings.approvers_json.get(
        learner_email.lower(), settings.default_approver_email
    )


# ---------------------------------------------------------------------------
# Links + e-mail
# ---------------------------------------------------------------------------
def decision_links(guide_id: str) -> dict[str, str]:
    base = get_settings().public_base_url_clean
    return {
        action: f"{base}/api/decision?" + urlencode({"token": sign(guide_id, action)})
        for action in _VALID_ACTIONS
    }


def _send_email(to_address: str, subject: str, html: str, plain: str) -> None:
    settings = get_settings()
    if settings.dev_email_to_console or not settings.acs_connection_string:
        logger.info(
            "\n----- EMAIL (console) -----\nTo: %s\nSubject: %s\n\n%s\n---------------------------",
            to_address,
            subject,
            plain,
        )
        return

    from azure.communication.email import EmailClient

    client = EmailClient.from_connection_string(settings.acs_connection_string)
    message = {
        "senderAddress": settings.acs_sender_address,
        "recipients": {"to": [{"address": to_address}]},
        "content": {"subject": subject, "plainText": plain, "html": html},
    }
    poller = client.begin_send(message)
    poller.result()


def request_approval(guide: GuideRecord) -> None:
    links = decision_links(guide.guide_id)
    g = guide.guide
    subject = f"Approval needed: study plan for {guide.learner_name or guide.learner_email}"
    plain = (
        f"{guide.learner_name or guide.learner_email} has a draft study plan awaiting your approval.\n\n"
        f"Target certification: {g.target_certification}\n"
        f"Exam(s): {', '.join(g.exam_codes) or 'n/a'}\n"
        f"Length: {len(g.weeks)} weeks at ~{g.weekly_hours} h/week\n\n"
        f"Rationale: {g.rationale}\n\n"
        f"APPROVE: {links['approve']}\n"
        f"REJECT:  {links['reject']}\n\n"
        f"These links expire in {get_settings().approval_link_ttl_hours} hours and can be used once."
    )
    html = (
        f"<p><strong>{guide.learner_name or guide.learner_email}</strong> has a draft study "
        f"plan awaiting your approval.</p>"
        f"<ul><li><strong>Target certification:</strong> {g.target_certification}</li>"
        f"<li><strong>Exam(s):</strong> {', '.join(g.exam_codes) or 'n/a'}</li>"
        f"<li><strong>Length:</strong> {len(g.weeks)} weeks at ~{g.weekly_hours} h/week</li></ul>"
        f"<p>{g.rationale}</p>"
        f'<p><a href="{links["approve"]}">✅ Approve</a>&nbsp;&nbsp;&nbsp;'
        f'<a href="{links["reject"]}">❌ Reject</a></p>'
        f"<p style='color:#666'>Links expire in "
        f"{get_settings().approval_link_ttl_hours} hours and can be used once.</p>"
    )
    _send_email(guide.approver_email, subject, html, plain)


def notify_learner(guide: GuideRecord) -> None:
    approved = guide.status == GuideStatus.APPROVED
    verb = "approved" if approved else "rejected"
    subject = f"Your study plan was {verb}"
    comment = f"\n\nComment from {guide.approver_email}: {guide.approver_comment}" if guide.approver_comment else ""
    plain = (
        f"Your study plan for {guide.guide.target_certification} was {verb} "
        f"by {guide.approver_email}.{comment}\n\n"
        + ("You're clear to start — good luck!" if approved else "Please revise and resubmit.")
    )
    html = f"<p>Your study plan for <strong>{guide.guide.target_certification}</strong> was <strong>{verb}</strong> by {guide.approver_email}.</p>"
    if guide.approver_comment:
        html += f"<p><em>Comment:</em> {guide.approver_comment}</p>"
    html += "<p>You're clear to start — good luck!</p>" if approved else "<p>Please revise and resubmit.</p>"
    _send_email(guide.learner_email, subject, html, plain)


# ---------------------------------------------------------------------------
# Decision handling
# ---------------------------------------------------------------------------
def apply_decision(token: str, comment: str = "") -> GuideRecord:
    from .storage import get_repo

    guide_id, action = verify(token)
    repo = get_repo()
    existing = repo.get_guide(guide_id)
    if existing is None:
        raise ApprovalError(f"unknown guide {guide_id}")
    if not existing.is_pending:
        raise AlreadyDecidedError(existing)

    status = GuideStatus.APPROVED if action == "approve" else GuideStatus.REJECTED
    updated = repo.set_decision(guide_id, status, comment)
    assert updated is not None
    try:
        notify_learner(updated)
    except Exception:  # noqa: BLE001 — notification failure must not fail the decision
        logger.exception("Failed to notify learner for guide %s", guide_id)
    return updated
