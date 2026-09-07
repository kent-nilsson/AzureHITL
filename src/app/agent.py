"""Azure AI Foundry Agent Service integration.

``run_study_guide`` runs a persistent Foundry agent that calls the
``search_learn_catalog`` tool (backed by the Microsoft Learn Catalog API) and
finishes by calling ``submit_study_guide`` with the structured plan.

When ``FAKE_AGENT`` is set (local dev / CI) the whole Foundry round-trip is
skipped and a plan is assembled directly from the catalog instead.
"""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar

from .config import get_settings
from .learn_catalog import get_catalog_client
from .models import IntakeAnswers, StudyGuide, StudyResource, StudyWeek
from .prompts import AGENT_INSTRUCTIONS, AGENT_NAME, INTAKE_TEMPLATE

logger = logging.getLogger(__name__)

# Collects the guide submitted by the agent during a single run.
_run_ctx: ContextVar[dict] = ContextVar("study_guide_run_ctx")

_STOPWORDS = {
    "the", "and", "for", "with", "want", "become", "certification", "certified",
    "microsoft", "azure", "exam", "get", "role", "targeting", "learn", "pass",
}


# ---------------------------------------------------------------------------
# Tool functions (invoked by the agent, executed locally)
# ---------------------------------------------------------------------------
def search_learn_catalog(
    type: str = "",
    role: str = "",
    product: str = "",
    subject: str = "",
    level: str = "",
    uid: str = "",
    q: str = "",
) -> str:
    """Search the Microsoft Learn catalog for real training content.

    :param type: Comma-separated content types: certifications, mergedCertifications,
        exams, learningPaths, modules, appliedSkills, courses.
    :param role: Comma-separated job roles, e.g. "developer,solution-architect".
    :param product: Comma-separated products, e.g. "azure,azure-functions".
    :param subject: Comma-separated subject tags, e.g. "cloud-computing".
    :param level: Comma-separated levels: beginner, intermediate, advanced.
    :param uid: Comma-separated exact content uids to fetch.
    :param q: Free-text filter matched against title, summary and skills.
    :return: JSON array of matching catalog records.
    """

    records = get_catalog_client().search(
        type=type or None,
        role=role or None,
        product=product or None,
        subject=subject or None,
        level=level or None,
        uid=uid or None,
        q=q or None,
    )
    return json.dumps(records, ensure_ascii=False)


def submit_study_guide(payload_json: str) -> str:
    """Submit the finished study guide for human approval.

    :param payload_json: The full study guide as a JSON string.
    :return: "accepted" once the guide validates.
    """

    data = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
    guide = StudyGuide.model_validate(data)
    try:
        _run_ctx.get()["guide"] = guide
    except LookupError:  # called outside a managed run (shouldn't happen)
        logger.warning("submit_study_guide called with no active run context")
    return "accepted"


_TOOLS = {search_learn_catalog, submit_study_guide}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def run_study_guide(answers: IntakeAnswers) -> StudyGuide:
    settings = get_settings()
    if settings.fake_agent or not settings.project_endpoint:
        logger.info("FAKE_AGENT active — building study guide from the catalog directly")
        return _fake_guide(answers)

    token = _run_ctx.set({"guide": None})
    try:
        _run_foundry_agent(answers)
        guide = _run_ctx.get()["guide"]
    finally:
        _run_ctx.reset(token)

    if guide is None:
        raise RuntimeError("Agent finished without calling submit_study_guide")
    return guide


# ---------------------------------------------------------------------------
# Foundry Agent Service
# ---------------------------------------------------------------------------
def _run_foundry_agent(answers: IntakeAnswers) -> None:
    from azure.ai.agents.models import FunctionTool, ToolSet
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    settings = get_settings()
    project = AIProjectClient(
        endpoint=settings.project_endpoint, credential=DefaultAzureCredential()
    )

    with project:
        agents = project.agents
        toolset = ToolSet()
        toolset.add(FunctionTool(_TOOLS))
        agents.enable_auto_function_calls(toolset)

        agent_id = settings.agent_id or _ensure_agent(agents, toolset)
        thread = agents.threads.create()
        agents.messages.create(
            thread_id=thread.id,
            role="user",
            content=INTAKE_TEMPLATE.format(
                certificates=answers.certificates,
                background=answers.background,
                goal=answers.goal,
            ),
        )
        run = agents.runs.create_and_process(
            thread_id=thread.id, agent_id=agent_id, toolset=toolset
        )
        logger.info("Foundry run %s finished: %s", run.id, run.status)
        if run.status == "failed":
            raise RuntimeError(f"Agent run failed: {run.last_error}")


def _ensure_agent(agents, toolset) -> str:
    """Reuse an agent named AGENT_NAME if it exists, else create one."""

    try:
        for existing in agents.list_agents():
            if existing.name == AGENT_NAME:
                return existing.id
    except Exception:  # noqa: BLE001 — listing is best-effort
        logger.debug("Could not list existing agents; creating a new one")

    settings = get_settings()
    agent = agents.create_agent(
        model=settings.model_deployment_name,
        name=AGENT_NAME,
        instructions=AGENT_INSTRUCTIONS,
        toolset=toolset,
    )
    return agent.id


# ---------------------------------------------------------------------------
# Offline fallback
# ---------------------------------------------------------------------------
def _keywords(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9-]{3,}", text.lower())
    return [w for w in words if w not in _STOPWORDS]


_EXAM_CODE_RE = re.compile(r"\b([A-Za-z]{2}-?\d{3})\b")


def _as_resource(rec: dict) -> StudyResource:
    return StudyResource(
        title=rec.get("title", rec.get("uid", "")),
        url=rec.get("url", ""),
        kind=rec.get("type", "module"),
        uid=rec.get("uid", ""),
        duration_minutes=rec.get("duration_in_minutes"),
    )


def _find_cert(client, answers: IntakeAnswers) -> dict | None:
    for kw in _keywords(answers.goal):
        hits = client.search(type="mergedCertifications,certifications", q=kw, limit=1)
        if hits:
            return hits[0]
    hits = client.search(type="mergedCertifications", product="azure", q="fundamentals", limit=1)
    return hits[0] if hits else None


def _find_exam_codes(client, answers: IntakeAnswers, cert: dict) -> list[str]:
    codes = [m.group(1).upper().replace("--", "-") for m in _EXAM_CODE_RE.finditer(answers.goal)]
    codes = [c if "-" in c else f"{c[:2]}-{c[2:]}" for c in codes]
    if codes:
        return list(dict.fromkeys(codes))
    for code_hit in client.search(
        type="exams", product=",".join(cert.get("products", []) or ["azure"]),
        role=",".join(cert.get("roles", [])) or None, limit=3,
    ):
        name = code_hit.get("display_name") or ""
        if _EXAM_CODE_RE.fullmatch(name):
            return [name.upper()]
    return []


def _gather_resources(client, cert: dict, limit: int = 8) -> list[StudyResource]:
    """Prefer content that matches a skill keyword; fall back to product/role."""

    products = ",".join(cert.get("products", []) or ["azure"])
    roles = ",".join(cert.get("roles", [])) or None
    levels = ",".join(cert.get("levels", [])) or None
    skill_terms = {
        w
        for skill in cert.get("skills", [])
        for w in _keywords(skill)
        if len(w) > 4
    }

    seen: set[str] = set()
    on_topic: list[StudyResource] = []
    generic: list[StudyResource] = []
    for content_type in ("learningPaths", "modules"):
        for rec in client.search(
            type=content_type, product=products, role=roles, level=levels, limit=40
        ):
            uid = rec.get("uid")
            if uid in seen:
                continue
            seen.add(uid)
            text = f"{rec.get('title', '')} {rec.get('summary', '')}".lower()
            bucket = on_topic if any(term in text for term in skill_terms) else generic
            bucket.append(_as_resource(rec))

    return (on_topic + generic)[:limit]


def _fake_guide(answers: IntakeAnswers) -> StudyGuide:
    client = get_catalog_client()
    cert = _find_cert(client, answers)

    if cert is None:
        return StudyGuide(
            target_certification="Microsoft Certified: Azure Fundamentals (AZ-900)",
            rationale="Catalog unavailable; showing a generic starter plan.",
            weeks=[StudyWeek(week=1, focus="Cloud concepts", activities=["Review Azure fundamentals"])],
        )

    exam_codes = _find_exam_codes(client, answers, cert)
    resources = _gather_resources(client, cert)
    skills = cert.get("skills", []) or ["Core concepts"]
    skill_weeks = skills[:5]

    weeks: list[StudyWeek] = []
    for idx, skill in enumerate(skill_weeks):
        chunk = resources[idx :: max(len(skill_weeks), 1)]
        weeks.append(
            StudyWeek(
                week=idx + 1,
                focus=skill,
                activities=[f"Study the skill area: {skill}"]
                + [f"Work through: {r.title}" for r in chunk],
                resources=chunk,
            )
        )
    weeks.append(
        StudyWeek(
            week=len(weeks) + 1,
            focus="Practice and exam readiness",
            activities=[
                "Take the free Microsoft practice assessment",
                "Review weak areas against the skills-measured list",
                f"Schedule exam {exam_codes[0]}" if exam_codes else "Schedule the exam",
            ],
        )
    )

    return StudyGuide(
        target_certification=cert.get("title", "Microsoft Certification"),
        certification_url=cert.get("url", ""),
        exam_codes=exam_codes,
        rationale=(
            f"Chosen from your goal ({answers.goal!r}). The plan is organized around the "
            f"certification's skills-measured areas and uses the official Microsoft Learn "
            f"learning paths for {', '.join(cert.get('products', [])) or 'Azure'}. "
            f"Weeks are sized to ~5 h/week; adjust earlier weeks down for areas you already "
            f"know from your background ({answers.background[:100]})."
        ),
        weekly_hours=5,
        weeks=weeks,
        resources=resources,
    )
