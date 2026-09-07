"""Microsoft Foundry study-guide generation.

``run_study_guide`` drives a Foundry model deployment through the project's
OpenAI-compatible client (``AIProjectClient.get_openai_client``), running a plain
function-calling loop: the model calls ``search_learn_catalog`` (backed by the
Microsoft Learn Catalog API) as many times as it needs, then finishes with
``submit_study_guide`` carrying the structured plan.

With ``FAKE_AGENT`` set (or no ``PROJECT_ENDPOINT``) the Foundry round-trip is
skipped and the plan is assembled directly from the catalog instead.

This deliberately avoids the ``azure-ai-agents`` threads/runs surface, which has
changed shape several times across SDK versions; the OpenAI chat-completions
tool-call loop is stable.
"""

from __future__ import annotations

import json
import logging
import re

from .config import get_settings
from .learn_catalog import get_catalog_client
from .models import IntakeAnswers, StudyGuide, StudyResource, StudyWeek
from .prompts import AGENT_INSTRUCTIONS, INTAKE_TEMPLATE

logger = logging.getLogger(__name__)

_MAX_TOOL_ROUNDS = 8


class ModelBusyError(RuntimeError):
    """The Foundry model deployment is rate-limited; the caller should retry shortly."""

_STOPWORDS = {
    "the", "and", "for", "with", "want", "become", "certification", "certified",
    "microsoft", "azure", "exam", "get", "role", "targeting", "learn", "pass",
}


# ---------------------------------------------------------------------------
# Tools exposed to the model
# ---------------------------------------------------------------------------
_RESOURCE_PROPS = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "url": {"type": "string"},
        "kind": {"type": "string", "description": "certification | exam | learningPath | module | other"},
        "uid": {"type": "string"},
        "duration_minutes": {"type": ["integer", "null"]},
    },
    "required": ["title", "url", "kind"],
}

_STUDY_GUIDE_PARAMS = {
    "type": "object",
    "properties": {
        "target_certification": {"type": "string"},
        "certification_url": {"type": "string"},
        "exam_codes": {"type": "array", "items": {"type": "string"}},
        "rationale": {
            "type": "string",
            "description": "2-3 sentences: why this cert, what you tailored to the learner",
        },
        "weekly_hours": {"type": "integer"},
        "weeks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "week": {"type": "integer"},
                    "focus": {"type": "string"},
                    "activities": {"type": "array", "items": {"type": "string"}},
                    "resources": {"type": "array", "items": _RESOURCE_PROPS},
                },
                "required": ["week", "focus", "activities"],
            },
        },
        "resources": {"type": "array", "items": _RESOURCE_PROPS},
    },
    "required": ["target_certification", "rationale", "weeks"],
}


def _search_learn_catalog(**kwargs: str) -> str:
    records = get_catalog_client().search(
        type=kwargs.get("type") or None,
        role=kwargs.get("role") or None,
        product=kwargs.get("product") or None,
        subject=kwargs.get("subject") or None,
        level=kwargs.get("level") or None,
        uid=kwargs.get("uid") or None,
        q=kwargs.get("q") or None,
    )
    return json.dumps(records, ensure_ascii=False)


_TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "search_learn_catalog",
            "description": (
                "Search the Microsoft Learn catalog for real certifications, exams, "
                "learning paths and modules. Multiple filters are AND-ed. Only cite "
                "uids and urls this returns."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "description": "comma-separated: certifications, mergedCertifications, exams, learningPaths, modules",
                    },
                    "role": {"type": "string", "description": "comma-separated job roles, e.g. developer"},
                    "product": {"type": "string", "description": "comma-separated products, e.g. azure,azure-functions"},
                    "subject": {"type": "string", "description": "comma-separated subject tags"},
                    "level": {"type": "string", "description": "beginner, intermediate or advanced"},
                    "uid": {"type": "string", "description": "exact content uid(s), comma-separated"},
                    "q": {"type": "string", "description": "free-text filter over title, summary and skills"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_study_guide",
            "description": "Submit the finished study guide for human approval. Call exactly once, last.",
            "parameters": _STUDY_GUIDE_PARAMS,
        },
    },
]


def _dispatch_tool(name: str, arguments: str, collector: dict) -> str:
    args = json.loads(arguments) if arguments else {}
    if name == "search_learn_catalog":
        return _search_learn_catalog(**args)
    if name == "submit_study_guide":
        payload = args.get("payload_json", args)
        if isinstance(payload, str):
            payload = json.loads(payload)
        collector["guide"] = StudyGuide.model_validate(payload)
        return "accepted"
    return f"error: unknown tool {name!r}"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def run_study_guide(answers: IntakeAnswers) -> StudyGuide:
    settings = get_settings()
    if settings.fake_agent or not settings.project_endpoint:
        logger.info("FAKE_AGENT active — building study guide from the catalog directly")
        return _fake_guide(answers)
    return _run_foundry(answers)


# ---------------------------------------------------------------------------
# Microsoft Foundry
# ---------------------------------------------------------------------------
def _run_foundry(answers: IntakeAnswers) -> StudyGuide:
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential
    from openai import APIConnectionError, APITimeoutError, RateLimitError

    settings = get_settings()
    collector: dict = {}
    messages: list[dict] = [
        {"role": "system", "content": AGENT_INSTRUCTIONS},
        {
            "role": "user",
            "content": INTAKE_TEMPLATE.format(
                certificates=answers.certificates,
                background=answers.background,
                goal=answers.goal,
            ),
        },
    ]

    with DefaultAzureCredential() as credential, AIProjectClient(
        endpoint=settings.project_endpoint, credential=credential
    ) as project:
        # max_retries: the OpenAI SDK backs off and honours Retry-After on 429s.
        openai_client = project.get_openai_client(max_retries=4)

        try:
            for _round in range(_MAX_TOOL_ROUNDS):
                completion = openai_client.chat.completions.create(
                    model=settings.model_deployment_name,
                    messages=messages,
                    tools=_TOOL_SPECS,
                    tool_choice="auto",
                )
                choice = completion.choices[0].message
                entry: dict = {"role": "assistant", "content": choice.content or ""}
                if choice.tool_calls:
                    entry["tool_calls"] = [tc.model_dump() for tc in choice.tool_calls]
                messages.append(entry)

                if not choice.tool_calls:
                    break

                for tc in choice.tool_calls:
                    try:
                        result = _dispatch_tool(
                            tc.function.name, tc.function.arguments, collector
                        )
                    except Exception as exc:  # noqa: BLE001 — report back to the model
                        logger.warning("tool %s failed: %s", tc.function.name, exc)
                        result = f"error: {exc}"
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": result}
                    )

                if "guide" in collector:
                    break
        except (RateLimitError, APITimeoutError, APIConnectionError) as exc:
            logger.warning("Foundry model unavailable: %s", exc)
            raise ModelBusyError(
                "The model is busy right now (rate limit). Wait about 20 seconds and "
                "send your goal again."
            ) from exc

    guide = collector.get("guide") or _guide_from_text(messages)
    if guide is None:
        raise RuntimeError("Foundry model finished without producing a study guide")
    logger.info("Foundry produced a plan for %s", guide.target_certification)
    return guide


def _guide_from_text(messages: list[dict]) -> StudyGuide | None:
    """Last resort: pull a JSON study guide out of the final assistant message."""

    for entry in reversed(messages):
        if entry.get("role") != "assistant" or not entry.get("content"):
            continue
        text = entry["content"]
        for candidate in re.findall(r"\{.*\}", text, re.DOTALL):
            try:
                return StudyGuide.model_validate_json(candidate)
            except Exception:  # noqa: BLE001
                continue
    return None


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
