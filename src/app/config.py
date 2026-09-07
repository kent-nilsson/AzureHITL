"""Application settings, loaded from environment variables / a local .env file."""

from __future__ import annotations

import json
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed view of every environment variable the app reads."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Azure AI Foundry ---------------------------------------------------
    project_endpoint: str = Field(default="", alias="PROJECT_ENDPOINT")
    model_deployment_name: str = Field(default="gpt-4o", alias="MODEL_DEPLOYMENT_NAME")
    agent_id: str = Field(default="", alias="AGENT_ID")
    fake_agent: bool = Field(default=False, alias="FAKE_AGENT")

    # --- State store ------------------------------------------------------
    storage_table_endpoint: str = Field(default="", alias="STORAGE_TABLE_ENDPOINT")

    # --- Approval e-mail (ACS) ------------------------------------------
    acs_connection_string: str = Field(default="", alias="ACS_CONNECTION_STRING")
    acs_sender_address: str = Field(
        default="DoNotReply@example.azurecomm.net", alias="ACS_SENDER_ADDRESS"
    )
    dev_email_to_console: bool = Field(default=True, alias="DEV_EMAIL_TO_CONSOLE")

    # --- Approval workflow ---------------------------------------------
    approval_signing_key: str = Field(
        default="change-me-in-production", alias="APPROVAL_SIGNING_KEY"
    )
    public_base_url: str = Field(default="http://localhost:8000", alias="PUBLIC_BASE_URL")
    approval_link_ttl_hours: int = Field(default=72, alias="APPROVAL_LINK_TTL_HOURS")
    default_approver_email: str = Field(
        default="boss@example.com", alias="DEFAULT_APPROVER_EMAIL"
    )
    approvers_json: dict[str, str] = Field(default_factory=dict, alias="APPROVERS_JSON")

    # --- Learner identity (local dev) ---------------------------------
    dev_user_email: str = Field(default="learner@example.com", alias="DEV_USER_EMAIL")
    dev_user_name: str = Field(default="Demo Learner", alias="DEV_USER_NAME")

    # --- Microsoft Learn Catalog API --------------------------------
    learn_catalog_url: str = Field(
        default="https://learn.microsoft.com/api/catalog/", alias="LEARN_CATALOG_URL"
    )
    learn_catalog_ttl_hours: int = Field(default=6, alias="LEARN_CATALOG_TTL_HOURS")

    @field_validator("approvers_json", mode="before")
    @classmethod
    def _parse_approvers(cls, value: object) -> dict[str, str]:
        if value in (None, "", {}):
            return {}
        if isinstance(value, dict):
            return {str(k).lower(): str(v) for k, v in value.items()}
        parsed = json.loads(str(value))
        return {str(k).lower(): str(v) for k, v in parsed.items()}

    @property
    def public_base_url_clean(self) -> str:
        return self.public_base_url.rstrip("/")

    @property
    def use_in_memory_storage(self) -> bool:
        return not self.storage_table_endpoint


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so config is parsed once per process."""

    return Settings()  # type: ignore[call-arg]
