"""Client for the public Microsoft Learn Catalog API.

    GET https://learn.microsoft.com/api/catalog/?type=...&role=...&product=...

No authentication is required. The full response is ~13 MB, so we fetch it once
and cache it in-process for ``LEARN_CATALOG_TTL_HOURS``; every filtered query is
served from that cache. Multiple filters are AND-ed, matching the API's own
semantics.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from .config import get_settings

# type -> the key it lives under in the catalog response
_TYPE_TO_KEY: dict[str, str] = {
    "certifications": "certifications",
    "mergedCertifications": "mergedCertifications",
    "exams": "exams",
    "learningPaths": "learningPaths",
    "modules": "modules",
    "appliedSkills": "appliedSkills",
    "courses": "courses",
}

# Fields worth handing to the model, per record type. Anything else is dropped
# to keep tool-result tokens low.
_KEEP_FIELDS = (
    "uid",
    "type",
    "title",
    "subtitle",
    "display_name",
    "summary",
    "url",
    "levels",
    "roles",
    "products",
    "subjects",
    "duration_in_minutes",
    "exam_duration_in_minutes",
    "exams",
    "skills",
    "study_guide",
    "prerequisites",
    "certification_type",
)


class CatalogClient:
    """Fetches and caches the Learn catalog, then answers filtered searches."""

    def __init__(self, url: str | None = None, ttl_seconds: int | None = None) -> None:
        settings = get_settings()
        self._url = url or settings.learn_catalog_url
        self._ttl = ttl_seconds if ttl_seconds is not None else settings.learn_catalog_ttl_hours * 3600
        self._lock = threading.Lock()
        self._cache: dict[str, Any] | None = None
        self._fetched_at = 0.0

    # -- catalog retrieval -------------------------------------------------
    def _catalog(self) -> dict[str, Any]:
        with self._lock:
            fresh = self._cache is not None and (time.monotonic() - self._fetched_at) < self._ttl
            if not fresh:
                resp = httpx.get(self._url, timeout=60.0, follow_redirects=True)
                resp.raise_for_status()
                self._cache = resp.json()
                self._fetched_at = time.monotonic()
            return self._cache  # type: ignore[return-value]

    def refresh(self) -> None:
        with self._lock:
            self._cache = None

    # -- search ----------------------------------------------------------
    def search(
        self,
        *,
        type: str | None = None,
        role: str | None = None,
        product: str | None = None,
        subject: str | None = None,
        level: str | None = None,
        uid: str | None = None,
        q: str | None = None,
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        """Return trimmed catalog records matching every supplied filter."""

        catalog = self._catalog()
        types = _split(type) or list(_TYPE_TO_KEY)
        roles, products = _split(role), _split(product)
        subjects, levels, uids = _split(subject), _split(level), _split(uid)
        needle = (q or "").strip().lower()

        results: list[dict[str, Any]] = []
        for t in types:
            key = _TYPE_TO_KEY.get(t)
            if not key:
                continue
            for rec in catalog.get(key, []):
                if uids and rec.get("uid") not in uids:
                    continue
                if roles and not _overlap(rec.get("roles"), roles):
                    continue
                if products and not _overlap(rec.get("products"), products):
                    continue
                if subjects and not _overlap(rec.get("subjects"), subjects):
                    continue
                if levels and not _overlap(rec.get("levels"), levels):
                    continue
                if needle and needle not in _haystack(rec):
                    continue
                results.append(_trim(rec))

        results.sort(key=lambda r: r.get("_popularity", 0.0), reverse=True)
        for r in results:
            r.pop("_popularity", None)
        return results[:limit]


def _split(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _overlap(have: Any, want: list[str]) -> bool:
    have_set = {str(x).lower() for x in have} if isinstance(have, list) else set()
    return any(w.lower() in have_set for w in want)


def _haystack(rec: dict[str, Any]) -> str:
    parts = [str(rec.get(f, "")) for f in ("title", "subtitle", "display_name", "summary")]
    if isinstance(rec.get("skills"), list):
        parts += [str(s) for s in rec["skills"]]
    return " ".join(parts).lower()


def _trim(rec: dict[str, Any]) -> dict[str, Any]:
    out = {k: rec[k] for k in _KEEP_FIELDS if k in rec}
    out["_popularity"] = rec.get("popularity", 0.0)
    return out


_default_client: CatalogClient | None = None


def get_catalog_client() -> CatalogClient:
    global _default_client
    if _default_client is None:
        _default_client = CatalogClient()
    return _default_client
