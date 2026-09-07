"""FastAPI application: intake chat, study-guide generation, and the approval gate."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from html import escape as _esc
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agent import ModelBusyError, run_study_guide
from .approvals import (
    AlreadyDecidedError,
    ApprovalError,
    ApprovalTokenError,
    apply_decision,
    request_approval,
    resolve_approver,
    verify as verify_token,
)
from .config import get_settings
from .identity import learner_from_request
from .models import (
    INTAKE_ORDER,
    STEP_PROMPTS,
    GuideRecord,
    GuideStatus,
    IntakeAnswers,
    SessionRecord,
    Step,
)
from .storage import get_repo

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("study_planner")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    logger.info(
        "Study Planner up. fake_agent=%s in_memory_storage=%s email_console=%s",
        settings.fake_agent or not settings.project_endpoint,
        settings.use_in_memory_storage,
        settings.dev_email_to_console,
    )
    yield


app = FastAPI(title="Study Planner with Human-in-the-Loop", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------
class SessionStart(BaseModel):
    session_id: str | None = None


class Message(BaseModel):
    session_id: str
    text: str


class GuideView(BaseModel):
    guide_id: str
    status: GuideStatus
    markdown: str
    target_certification: str
    approver_email: str
    approver_comment: str = ""
    decided_at: str | None = None

    @classmethod
    def of(cls, rec: GuideRecord) -> "GuideView":
        return cls(
            guide_id=rec.guide_id,
            status=rec.status,
            markdown=rec.guide.to_markdown(),
            target_certification=rec.guide.target_certification,
            approver_email=rec.approver_email,
            approver_comment=rec.approver_comment,
            decided_at=rec.decided_at.isoformat() if rec.decided_at else None,
        )


class ChatTurn(BaseModel):
    role: str  # "bot" | "user"
    text: str


class StateView(BaseModel):
    session_id: str
    step: Step
    prompt: str | None
    answers: IntakeAnswers
    history: list[ChatTurn] = []
    guide: GuideView | None = None


_STEP_FIELD = {
    Step.ASK_CERTS: "certificates",
    Step.ASK_BACKGROUND: "background",
    Step.ASK_GOAL: "goal",
}


def _history(session: SessionRecord) -> list[ChatTurn]:
    """Replay already-answered intake questions so a resumed session reads cleanly."""

    turns: list[ChatTurn] = []
    for step in INTAKE_ORDER:
        answer = getattr(session.answers, _STEP_FIELD[step])
        if answer:
            turns.append(ChatTurn(role="bot", text=STEP_PROMPTS[step]))
            turns.append(ChatTurn(role="user", text=answer))
    return turns


def _state(session: SessionRecord) -> StateView:
    guide = None
    if session.guide_id:
        rec = get_repo().get_guide(session.guide_id)
        guide = GuideView.of(rec) if rec else None
    return StateView(
        session_id=session.session_id,
        step=session.step,
        prompt=STEP_PROMPTS.get(session.step),
        answers=session.answers,
        history=_history(session),
        guide=guide,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/session", response_model=StateView)
def start_session(body: SessionStart, request: Request) -> StateView:
    repo = get_repo()
    learner = learner_from_request(request)

    if body.session_id:
        existing = repo.get_session(body.session_id)
        if existing:
            return _state(existing)

    session = SessionRecord(learner_email=learner.email, learner_name=learner.name)
    repo.put_session(session)
    logger.info("New session %s for %s", session.session_id, learner.email)
    return _state(session)


@app.get("/api/session/{session_id}", response_model=StateView)
def get_session(session_id: str) -> StateView:
    session = get_repo().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return _state(session)


@app.post("/api/message", response_model=StateView)
def post_message(body: Message) -> StateView:
    repo = get_repo()
    session = repo.get_session(body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty answer")
    if session.step not in INTAKE_ORDER:
        raise HTTPException(
            status_code=409, detail=f"intake already complete (step={session.step.value})"
        )

    if session.step == Step.ASK_CERTS:
        session.answers.certificates = text
        session.step = Step.ASK_BACKGROUND
    elif session.step == Step.ASK_BACKGROUND:
        session.answers.background = text
        session.step = Step.ASK_GOAL
    elif session.step == Step.ASK_GOAL:
        session.answers.goal = text
        session.step = Step.GENERATING
    repo.put_session(session)

    if session.step == Step.GENERATING:
        _generate_and_submit(session)

    return _state(session)


def _generate_and_submit(session: SessionRecord) -> None:
    repo = get_repo()
    try:
        guide = run_study_guide(session.answers)
    except ModelBusyError as exc:
        logger.warning("Model busy for %s: %s", session.session_id, exc)
        session.step = Step.ASK_GOAL
        repo.put_session(session)
        raise HTTPException(
            status_code=503, detail=str(exc), headers={"Retry-After": "20"}
        )
    except Exception:
        logger.exception("Study guide generation failed for %s", session.session_id)
        session.step = Step.ASK_GOAL  # let the learner retry the goal
        repo.put_session(session)
        raise HTTPException(status_code=502, detail="study guide generation failed")

    record = GuideRecord(
        session_id=session.session_id,
        learner_email=session.learner_email,
        learner_name=session.learner_name,
        approver_email=resolve_approver(session.learner_email),
        guide=guide,
    )
    repo.put_guide(record)
    session.guide_id = record.guide_id
    session.step = Step.PENDING_APPROVAL
    repo.put_session(session)

    try:
        request_approval(record)
    except Exception:  # noqa: BLE001 — email failure shouldn't lose the guide
        logger.exception("Failed to send approval email for guide %s", record.guide_id)


@app.get("/api/guides/{guide_id}", response_model=GuideView)
def get_guide(guide_id: str) -> GuideView:
    rec = get_repo().get_guide(guide_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="guide not found")
    return GuideView.of(rec)


@app.get("/api/decision", response_class=HTMLResponse)
def decision_confirm(token: str = "") -> HTMLResponse:
    """Landing page for the email link.

    This is a **safe GET**: it only shows a confirmation form. The actual
    approve/reject happens on the POST below, so link scanners and mail-security
    prefetchers that follow the URL cannot change the decision.
    """

    try:
        guide_id, action = verify_token(token)
    except ApprovalTokenError as exc:
        return _decision_page("Invalid or expired link", str(exc), status_code=400)

    rec = get_repo().get_guide(guide_id)
    if rec is None:
        return _decision_page("Not found", "That study plan no longer exists.", status_code=404)
    if not rec.is_pending:
        return _decision_page(
            f"Already {rec.status.value.lower()}",
            f"This plan was already {rec.status.value.lower()}. Approval links are single-use.",
            status_code=409,
        )
    return _confirm_page(token, action, rec)


@app.post("/api/decision", response_class=HTMLResponse)
def decision_submit(token: str = Form(""), comment: str = Form("")) -> HTMLResponse:
    try:
        updated = apply_decision(token, comment=comment)
    except AlreadyDecidedError as exc:
        return _decision_page(
            f"Already {exc.guide.status.value.lower()}",
            f"This plan was already {exc.guide.status.value.lower()}. Approval links are single-use.",
            status_code=409,
        )
    except ApprovalTokenError as exc:
        return _decision_page("Invalid or expired link", str(exc), status_code=400)
    except ApprovalError as exc:
        return _decision_page("Could not record decision", str(exc), status_code=400)

    verb = "approved" if updated.status == GuideStatus.APPROVED else "rejected"
    return _decision_page(
        f"Plan {verb} ✅" if verb == "approved" else f"Plan {verb}",
        f"You {verb} the study plan for "
        f"{_esc(updated.learner_name or updated.learner_email)} "
        f"({_esc(updated.guide.target_certification)}). They have been notified.",
    )


_PAGE_CSS = (
    "font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;"
    "max-width:34rem;margin:4rem auto;padding:2rem;border:1px solid #ddd;border-radius:12px"
)


def _decision_page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    html = f"""<!doctype html><meta charset="utf-8"><title>{_esc(title)}</title>
<div style="{_PAGE_CSS}">
  <h1 style="margin-top:0">{_esc(title)}</h1>
  <p style="color:#444;line-height:1.5">{body}</p>
</div>"""
    return HTMLResponse(content=html, status_code=status_code)


def _confirm_page(token: str, action: str, rec: GuideRecord) -> HTMLResponse:
    approve = action == "approve"
    verb = "Approve" if approve else "Reject"
    colour = "#1f7a3d" if approve else "#b3261e"
    learner = _esc(rec.learner_name or rec.learner_email)
    cert = _esc(rec.guide.target_certification)
    weeks = len(rec.guide.weeks)
    html = f"""<!doctype html><meta charset="utf-8"><title>{verb} study plan</title>
<div style="{_PAGE_CSS}">
  <h1 style="margin-top:0">{verb} this study plan?</h1>
  <p style="color:#444;line-height:1.5">
    <strong>{learner}</strong> &mdash; {cert}<br>
    {weeks} weeks &middot; ~{rec.guide.weekly_hours} h/week
  </p>
  <p style="color:#444;line-height:1.5">{_esc(rec.guide.rationale)}</p>
  <form method="post" action="/api/decision">
    <input type="hidden" name="token" value="{_esc(token)}">
    <textarea name="comment" rows="3" placeholder="Optional note to the learner"
      style="width:100%;box-sizing:border-box;padding:.5rem;margin:.5rem 0;
             border:1px solid #ccc;border-radius:8px;font:inherit"></textarea>
    <button type="submit"
      style="background:{colour};color:#fff;border:0;border-radius:8px;
             padding:.7rem 1.4rem;font:inherit;cursor:pointer">
      Confirm &mdash; {verb}
    </button>
  </form>
</div>"""
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# Static chat UI (mounted last so /api/* wins)
# ---------------------------------------------------------------------------
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
