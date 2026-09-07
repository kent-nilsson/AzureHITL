"""The Foundry tool-call loop in agent._run_foundry, with a faked OpenAI client."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import agent, config
from app.models import IntakeAnswers, StudyGuide


# --- fakes ---------------------------------------------------------------
class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = call_id
        self.type = "function"
        self.function = SimpleNamespace(name=name, arguments=arguments)

    def model_dump(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }


def _msg(content: str = "", tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


class _FakeCompletions:
    def __init__(self, script: list) -> None:
        self.script = script
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=self.script[len(self.calls) - 1])])


class _FakeOpenAI:
    def __init__(self, script: list) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(script))


class _FakeProject:
    def __init__(self, script: list) -> None:
        self.openai = _FakeOpenAI(script)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get_openai_client(self):
        return self.openai


class _FakeCredential:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def foundry(monkeypatch):
    monkeypatch.setenv("FAKE_AGENT", "false")
    monkeypatch.setenv("PROJECT_ENDPOINT", "https://acc.services.ai.azure.com/api/projects/p")
    monkeypatch.setenv("MODEL_DEPLOYMENT_NAME", "gpt-4o")
    config.get_settings.cache_clear()

    monkeypatch.setattr(
        agent, "_search_learn_catalog",
        lambda **kw: json.dumps([{"uid": "learn.az204-fn", "title": "Implement Azure Functions",
                                  "url": "https://learn.microsoft.com/training/paths/x/", "type": "learningPath"}]),
    )

    holder: dict = {}

    def _install(script):
        proj = _FakeProject(script)
        holder["project"] = proj
        monkeypatch.setattr("azure.ai.projects.AIProjectClient", lambda **kw: proj)
        monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda *a, **k: _FakeCredential())
        return holder

    return _install


_GUIDE = {
    "target_certification": "Microsoft Certified: Azure Developer Associate",
    "certification_url": "https://learn.microsoft.com/credentials/certifications/azure-developer/",
    "exam_codes": ["AZ-204"],
    "rationale": "Fits the learner's Python background and AZ-204 goal.",
    "weekly_hours": 6,
    "weeks": [
        {"week": 1, "focus": "Compute", "activities": ["Do the Functions path"],
         "resources": [{"title": "Implement Azure Functions",
                        "url": "https://learn.microsoft.com/training/paths/x/", "kind": "learningPath",
                        "uid": "learn.az204-fn", "duration_minutes": 180}]},
    ],
    "resources": [],
}


def test_tool_loop_runs_search_then_submit(foundry):
    script = [
        _msg(tool_calls=[_FakeToolCall("c1", "search_learn_catalog",
                                       '{"type":"mergedCertifications","q":"azure developer"}')]),
        _msg(tool_calls=[_FakeToolCall("c2", "submit_study_guide", json.dumps(_GUIDE))]),
    ]
    holder = foundry(script)

    guide = agent.run_study_guide(
        IntakeAnswers(certificates="AZ-900", background="Python dev", goal="AZ-204")
    )

    assert isinstance(guide, StudyGuide)
    assert guide.target_certification == _GUIDE["target_certification"]
    assert guide.exam_codes == ["AZ-204"]

    calls = holder["project"].openai.chat.completions.calls
    assert len(calls) == 2
    # second call must carry the tool result for c1
    roles = [m["role"] for m in calls[1]["messages"]]
    assert "tool" in roles
    tool_msg = next(m for m in calls[1]["messages"] if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "c1"


def test_guide_recovered_from_final_message_if_no_submit_call(foundry):
    script = [
        _msg(content="Here is the plan:\n```json\n" + json.dumps(_GUIDE) + "\n```"),
    ]
    foundry(script)

    guide = agent.run_study_guide(
        IntakeAnswers(certificates="none", background="new", goal="AZ-204")
    )
    assert guide.target_certification == _GUIDE["target_certification"]


def test_raises_when_no_guide_produced(foundry):
    foundry([_msg(content="I could not find a suitable certification.")])

    with pytest.raises(RuntimeError, match="without producing a study guide"):
        agent.run_study_guide(IntakeAnswers(certificates="x", background="y", goal="z"))
