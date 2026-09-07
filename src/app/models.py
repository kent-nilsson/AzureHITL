"""Pydantic models: intake answers, the generated study guide, and stored records."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Session state machine
# ---------------------------------------------------------------------------
class Step(str, Enum):
    ASK_CERTS = "ASK_CERTS"
    ASK_BACKGROUND = "ASK_BACKGROUND"
    ASK_GOAL = "ASK_GOAL"
    GENERATING = "GENERATING"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


# The prompt shown to the learner for each intake step, asked one at a time.
STEP_PROMPTS: dict[Step, str] = {
    Step.ASK_CERTS: (
        "Which Microsoft (or other) certifications do you currently hold? "
        "List them, or say 'none'."
    ),
    Step.ASK_BACKGROUND: (
        "Tell me about your technical background: your role, years of experience, "
        "and the Azure or Microsoft technologies you have worked with."
    ),
    Step.ASK_GOAL: (
        "What is your goal? For example a certification you want to earn, a role you "
        "are targeting, or a deadline you are working toward."
    ),
}

INTAKE_ORDER: list[Step] = [Step.ASK_CERTS, Step.ASK_BACKGROUND, Step.ASK_GOAL]


class IntakeAnswers(BaseModel):
    certificates: str = ""
    background: str = ""
    goal: str = ""

    def is_complete(self) -> bool:
        return bool(self.certificates and self.background and self.goal)


# ---------------------------------------------------------------------------
# Study guide (what the agent produces)
# ---------------------------------------------------------------------------
class StudyResource(BaseModel):
    title: str
    url: str
    kind: str = Field(description="certification | exam | learningPath | module | other")
    uid: str = ""
    duration_minutes: int | None = None


class StudyWeek(BaseModel):
    week: int
    focus: str
    activities: list[str] = Field(default_factory=list)
    resources: list[StudyResource] = Field(default_factory=list)


class StudyGuide(BaseModel):
    target_certification: str
    certification_url: str = ""
    exam_codes: list[str] = Field(default_factory=list)
    rationale: str = ""
    weekly_hours: int = 5
    weeks: list[StudyWeek] = Field(default_factory=list)
    resources: list[StudyResource] = Field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [f"# Study plan: {self.target_certification}"]
        if self.certification_url:
            lines.append(f"\n[Certification page]({self.certification_url})")
        if self.exam_codes:
            lines.append(f"\n**Exam(s):** {', '.join(self.exam_codes)}")
        if self.rationale:
            lines.append(f"\n{self.rationale}")
        lines.append(f"\n**Suggested effort:** ~{self.weekly_hours} hours/week\n")
        for wk in self.weeks:
            lines.append(f"\n## Week {wk.week} — {wk.focus}")
            for act in wk.activities:
                lines.append(f"- {act}")
            for res in wk.resources:
                dur = f" ({res.duration_minutes} min)" if res.duration_minutes else ""
                lines.append(f"- [{res.title}]({res.url}){dur}")
        if self.resources:
            lines.append("\n## All resources")
            for res in self.resources:
                lines.append(f"- [{res.title}]({res.url}) — {res.kind}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stored records (Azure Table Storage entities)
# ---------------------------------------------------------------------------
class GuideStatus(str, Enum):
    PENDING_APPROVAL = "PendingApproval"
    APPROVED = "Approved"
    REJECTED = "Rejected"


class SessionRecord(BaseModel):
    session_id: str = Field(default_factory=lambda: new_id("sess"))
    learner_email: str = ""
    learner_name: str = ""
    step: Step = Step.ASK_CERTS
    answers: IntakeAnswers = Field(default_factory=IntakeAnswers)
    guide_id: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class GuideRecord(BaseModel):
    guide_id: str = Field(default_factory=lambda: new_id("guide"))
    session_id: str
    learner_email: str
    learner_name: str = ""
    approver_email: str
    status: GuideStatus = GuideStatus.PENDING_APPROVAL
    guide: StudyGuide
    approver_comment: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    decided_at: datetime | None = None

    @property
    def is_pending(self) -> bool:
        return self.status == GuideStatus.PENDING_APPROVAL
