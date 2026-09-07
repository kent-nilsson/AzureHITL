"""Resolve the signed-in learner.

In Azure, App Service Easy Auth injects the authenticated principal as a base64
JSON blob in the ``X-MS-CLIENT-PRINCIPAL`` header (plus convenience headers
``X-MS-CLIENT-PRINCIPAL-NAME`` / ``-ID``). Locally we fall back to the
``DEV_USER_*`` settings so the app runs without an identity provider.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from starlette.requests import Request

from .config import get_settings

_EMAIL_CLAIMS = (
    "preferred_username",
    "emails",
    "email",
    "upn",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
)
_NAME_CLAIMS = (
    "name",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
)


@dataclass(frozen=True)
class Learner:
    email: str
    name: str


def _claims_map(principal: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for claim in principal.get("claims", []):
        typ = claim.get("typ") or claim.get("type")
        val = claim.get("val") or claim.get("value")
        if typ and val and typ not in out:
            out[typ] = val
    return out


def learner_from_request(request: Request) -> Learner:
    settings = get_settings()
    header = request.headers.get("x-ms-client-principal")
    if header:
        try:
            principal = json.loads(base64.b64decode(header).decode("utf-8"))
            claims = _claims_map(principal)
            email = next((claims[c] for c in _EMAIL_CLAIMS if c in claims), "")
            name = next((claims[c] for c in _NAME_CLAIMS if c in claims), "")
            email = email or request.headers.get("x-ms-client-principal-name", "")
            if email:
                return Learner(email=email.lower(), name=name or email)
        except (ValueError, binascii.Error, json.JSONDecodeError):
            pass

    name_header = request.headers.get("x-ms-client-principal-name")
    if name_header:
        return Learner(email=name_header.lower(), name=name_header)

    return Learner(email=settings.dev_user_email.lower(), name=settings.dev_user_name)
