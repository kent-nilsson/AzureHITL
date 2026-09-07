"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Deterministic local config for every test."""

    monkeypatch.setenv("FAKE_AGENT", "true")
    monkeypatch.setenv("PROJECT_ENDPOINT", "")
    monkeypatch.setenv("STORAGE_TABLE_ENDPOINT", "")
    monkeypatch.setenv("DEV_EMAIL_TO_CONSOLE", "true")
    monkeypatch.setenv("ACS_CONNECTION_STRING", "")
    monkeypatch.setenv("APPROVAL_SIGNING_KEY", "test-signing-key")
    monkeypatch.setenv("APPROVAL_LINK_TTL_HOURS", "72")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://testserver")
    monkeypatch.setenv("DEFAULT_APPROVER_EMAIL", "boss@example.com")
    monkeypatch.setenv("APPROVERS_JSON", '{"vip@example.com": "cto@example.com"}')
    monkeypatch.setenv("DEV_USER_EMAIL", "learner@example.com")
    monkeypatch.setenv("DEV_USER_NAME", "Test Learner")

    from app import config, storage

    config.get_settings.cache_clear()
    storage.reset_repo_for_tests()
    yield
    config.get_settings.cache_clear()
    storage.reset_repo_for_tests()


@pytest.fixture
def sample_catalog() -> dict:
    return {
        "certifications": [],
        "mergedCertifications": [
            {
                "uid": "certification.azure-developer",
                "type": "cert",
                "title": "Microsoft Certified: Azure Developer Associate",
                "summary": "Build cloud apps and services on Azure.",
                "url": "https://learn.microsoft.com/credentials/certifications/azure-developer/",
                "levels": ["intermediate"],
                "roles": ["developer"],
                "products": ["azure"],
                "subjects": ["cloud-computing"],
                "popularity": 0.91,
                "exams": ["exam.az-204"],
                "skills": ["Develop Azure compute solutions", "Implement Azure Functions"],
                "study_guide": [
                    {"uid": "learn.az204-functions", "type": "learningPath"},
                    {"uid": "learn.az204-storage", "type": "module"},
                ],
            }
        ],
        "exams": [
            {
                "uid": "exam.az-204",
                "type": "exam",
                "title": "Developing Solutions for Microsoft Azure",
                "display_name": "AZ-204",
                "url": "https://learn.microsoft.com/credentials/certifications/exams/az-204/",
                "roles": ["developer"],
                "products": ["azure"],
                "levels": ["intermediate"],
                "popularity": 0.8,
            }
        ],
        "learningPaths": [
            {
                "uid": "learn.az204-functions",
                "type": "learningPath",
                "title": "Implement Azure Functions",
                "summary": "Create serverless apps with Azure Functions.",
                "url": "https://learn.microsoft.com/training/paths/implement-azure-functions/",
                "levels": ["intermediate"],
                "roles": ["developer"],
                "products": ["azure", "azure-functions"],
                "duration_in_minutes": 180,
                "popularity": 0.77,
            }
        ],
        "modules": [
            {
                "uid": "learn.az204-storage",
                "type": "module",
                "title": "Develop solutions that use Blob storage",
                "summary": "Work with Azure Blob storage from code.",
                "url": "https://learn.microsoft.com/training/modules/develop-solutions-that-use-blob-storage/",
                "levels": ["intermediate"],
                "roles": ["developer"],
                "products": ["azure", "azure-blob-storage"],
                "duration_in_minutes": 90,
                "popularity": 0.6,
            },
            {
                "uid": "learn.py-basics",
                "type": "module",
                "title": "Python basics",
                "summary": "Intro to Python.",
                "url": "https://learn.microsoft.com/training/modules/python-basics/",
                "levels": ["beginner"],
                "roles": ["developer"],
                "products": ["python"],
                "duration_in_minutes": 60,
                "popularity": 0.95,
            },
        ],
    }
