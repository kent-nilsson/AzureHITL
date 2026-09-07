"""Persistence for sessions and study guides.

Uses Azure Table Storage when ``STORAGE_TABLE_ENDPOINT`` is set, otherwise an
in-process dict (fine for local dev and tests). Both back-ends expose the same
small ``Repo`` interface.
"""

from __future__ import annotations

import threading
from typing import Protocol

from .config import get_settings
from .models import GuideRecord, GuideStatus, SessionRecord

_SESSIONS_TABLE = "sessions"
_GUIDES_TABLE = "guides"


class Repo(Protocol):
    def get_session(self, session_id: str) -> SessionRecord | None: ...
    def put_session(self, session: SessionRecord) -> None: ...
    def get_guide(self, guide_id: str) -> GuideRecord | None: ...
    def put_guide(self, guide: GuideRecord) -> None: ...
    def set_decision(
        self, guide_id: str, status: GuideStatus, comment: str = ""
    ) -> GuideRecord | None: ...


# ---------------------------------------------------------------------------
# In-memory
# ---------------------------------------------------------------------------
class InMemoryRepo:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionRecord] = {}
        self._guides: dict[str, GuideRecord] = {}
        self._lock = threading.Lock()

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._lock:
            rec = self._sessions.get(session_id)
            return rec.model_copy(deep=True) if rec else None

    def put_session(self, session: SessionRecord) -> None:
        with self._lock:
            self._sessions[session.session_id] = session.model_copy(deep=True)

    def get_guide(self, guide_id: str) -> GuideRecord | None:
        with self._lock:
            rec = self._guides.get(guide_id)
            return rec.model_copy(deep=True) if rec else None

    def put_guide(self, guide: GuideRecord) -> None:
        with self._lock:
            self._guides[guide.guide_id] = guide.model_copy(deep=True)

    def set_decision(
        self, guide_id: str, status: GuideStatus, comment: str = ""
    ) -> GuideRecord | None:
        from datetime import datetime, timezone

        with self._lock:
            rec = self._guides.get(guide_id)
            if rec is None:
                return None
            rec.status = status
            rec.approver_comment = comment
            rec.decided_at = datetime.now(timezone.utc)
            return rec.model_copy(deep=True)


# ---------------------------------------------------------------------------
# Azure Table Storage
# ---------------------------------------------------------------------------
class TableRepo:
    def __init__(self, endpoint: str) -> None:
        from azure.data.tables import TableServiceClient
        from azure.identity import DefaultAzureCredential

        self._svc = TableServiceClient(
            endpoint=endpoint, credential=DefaultAzureCredential()
        )
        for name in (_SESSIONS_TABLE, _GUIDES_TABLE):
            self._svc.create_table_if_not_exists(name)
        self._sessions = self._svc.get_table_client(_SESSIONS_TABLE)
        self._guides = self._svc.get_table_client(_GUIDES_TABLE)

    # -- sessions ------------------------------------------------------
    def get_session(self, session_id: str) -> SessionRecord | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            entity = self._sessions.get_entity("session", session_id)
        except ResourceNotFoundError:
            return None
        return SessionRecord.model_validate_json(entity["data"])

    def put_session(self, session: SessionRecord) -> None:
        self._sessions.upsert_entity(
            {
                "PartitionKey": "session",
                "RowKey": session.session_id,
                "learner_email": session.learner_email,
                "step": session.step.value,
                "data": session.model_dump_json(),
            }
        )

    # -- guides -------------------------------------------------------
    def get_guide(self, guide_id: str) -> GuideRecord | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            entity = self._guides.get_entity("guide", guide_id)
        except ResourceNotFoundError:
            return None
        return GuideRecord.model_validate_json(entity["data"])

    def put_guide(self, guide: GuideRecord) -> None:
        self._guides.upsert_entity(_guide_entity(guide))

    def set_decision(
        self, guide_id: str, status: GuideStatus, comment: str = ""
    ) -> GuideRecord | None:
        from datetime import datetime, timezone

        rec = self.get_guide(guide_id)
        if rec is None:
            return None
        rec.status = status
        rec.approver_comment = comment
        rec.decided_at = datetime.now(timezone.utc)
        self._guides.upsert_entity(_guide_entity(rec))
        return rec


def _guide_entity(guide: GuideRecord) -> dict:
    return {
        "PartitionKey": "guide",
        "RowKey": guide.guide_id,
        "session_id": guide.session_id,
        "learner_email": guide.learner_email,
        "approver_email": guide.approver_email,
        "status": guide.status.value,
        "data": guide.model_dump_json(),
    }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
_repo: Repo | None = None
_repo_lock = threading.Lock()


def get_repo() -> Repo:
    global _repo
    if _repo is None:
        with _repo_lock:
            if _repo is None:
                settings = get_settings()
                if settings.use_in_memory_storage:
                    _repo = InMemoryRepo()
                else:
                    _repo = TableRepo(settings.storage_table_endpoint)
    return _repo


def reset_repo_for_tests() -> None:
    global _repo
    _repo = None
