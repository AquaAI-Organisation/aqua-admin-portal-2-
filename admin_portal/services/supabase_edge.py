"""Supabase Edge Function helpers for server-to-server AI orchestration."""
from __future__ import annotations

import json
from dataclasses import dataclass
from urllib import error, request

from django.conf import settings


def _strip(value) -> str:
    return str(value or "").strip()


@dataclass
class EdgeInvocationResult:
    ok: bool
    payload: dict | None = None
    error: str = ""
    status_code: int = 0


def _auth_token() -> str:
    return (
        _strip(getattr(settings, "SUPABASE_EDGE_AUTH_TOKEN", ""))
        or _strip(getattr(settings, "SUPABASE_SERVICE_KEY", ""))
    )


def signup_review_url() -> str:
    return (
        _strip(getattr(settings, "SUPABASE_FUNCTION_SIGNUP_REVIEW_URL", ""))
        or _strip(getattr(settings, "OPENAI_EDGE_FUNCTION_URL", ""))
    )


def issue_triage_url() -> str:
    return _strip(getattr(settings, "SUPABASE_FUNCTION_ISSUE_TRIAGE_URL", ""))


def inquiry_triage_url() -> str:
    return _strip(getattr(settings, "SUPABASE_FUNCTION_INQUIRY_TRIAGE_URL", ""))


def has_signup_review_function() -> bool:
    return bool(signup_review_url())


def has_issue_triage_function() -> bool:
    return bool(issue_triage_url())


def has_inquiry_triage_function() -> bool:
    return bool(inquiry_triage_url())


def invoke_json(url: str, payload: dict, *, timeout: int = 25) -> EdgeInvocationResult:
    token = _auth_token()
    headers = {
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["apikey"] = token

    req = request.Request(
        url,
        data=json.dumps(payload, default=str).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace").strip()
            data = json.loads(body) if body else {}
            if isinstance(data, dict):
                return EdgeInvocationResult(ok=True, payload=data, status_code=response.status)
            return EdgeInvocationResult(ok=False, error="Edge function returned a non-object JSON payload.", status_code=response.status)
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        return EdgeInvocationResult(ok=False, error=f"HTTP {exc.code}: {detail or exc.reason}", status_code=exc.code)
    except json.JSONDecodeError as exc:
        return EdgeInvocationResult(ok=False, error=f"Edge function returned invalid JSON: {exc}")
    except Exception as exc:  # pragma: no cover - network/runtime dependent
        return EdgeInvocationResult(ok=False, error=str(exc))
