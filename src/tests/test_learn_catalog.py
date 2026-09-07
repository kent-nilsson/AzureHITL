"""Tests for the Microsoft Learn Catalog API client."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.learn_catalog import CatalogClient

URL = "https://learn.microsoft.com/api/catalog/"


@pytest.fixture
def client(sample_catalog):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(URL).mock(return_value=httpx.Response(200, json=sample_catalog))
        c = CatalogClient(url=URL, ttl_seconds=3600)
        yield c, route


def test_filter_by_type_and_role(client):
    c, _ = client
    results = c.search(type="learningPaths", role="developer")
    assert [r["uid"] for r in results] == ["learn.az204-functions"]
    assert results[0]["type"] == "learningPath"


def test_free_text_matches_skills(client):
    c, _ = client
    results = c.search(type="mergedCertifications", q="azure functions")
    assert results and results[0]["uid"] == "certification.azure-developer"


def test_product_filter_excludes_nonmatching(client):
    c, _ = client
    results = c.search(type="modules", product="python")
    assert [r["uid"] for r in results] == ["learn.py-basics"]


def test_results_are_trimmed(client):
    c, _ = client
    (rec,) = c.search(type="modules", uid="learn.az204-storage")
    assert "popularity" not in rec and "_popularity" not in rec
    assert set(rec) <= {
        "uid", "type", "title", "subtitle", "summary", "url", "levels", "roles",
        "products", "subjects", "duration_in_minutes", "exam_duration_in_minutes",
        "exams", "skills", "study_guide", "prerequisites", "certification_type",
    }


def test_sorted_by_popularity(client):
    c, _ = client
    results = c.search(type="modules")
    assert [r["uid"] for r in results] == ["learn.py-basics", "learn.az204-storage"]


def test_catalog_fetched_once_then_cached(client):
    c, route = client
    c.search(type="modules")
    c.search(type="exams")
    c.search(type="mergedCertifications", q="developer")
    assert route.call_count == 1

    c.refresh()
    c.search(type="modules")
    assert route.call_count == 2
