"""Bridge to the AquaAI backend's content-moderation API.

Where user reports of objectionable content land so an admin can act on them
(App Store Guideline 1.2). Mirrors feature_d_backend.py: same configuration,
auth header and error handling.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import requests
from django.conf import settings


class ModerationBackendError(Exception):
    pass


def is_configured() -> bool:
    return bool(
        getattr(settings, "AQUAAI_BACKEND_API_URL", "").strip()
        and getattr(settings, "AQUAAI_BACKEND_API_TOKEN", "").strip()
    )


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.AQUAAI_BACKEND_API_TOKEN}",
        "Content-Type": "application/json",
    }


def _endpoint(path: str) -> str:
    base = settings.AQUAAI_BACKEND_API_URL.rstrip("/")
    return f"{base}{path}"


def _request(method: str, path: str, payload: Optional[Dict[str, Any]] = None,
             params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not is_configured():
        raise ModerationBackendError(
            "Backend bridge is not configured. Set AQUAAI_BACKEND_API_URL and AQUAAI_BACKEND_API_TOKEN."
        )
    try:
        response = requests.request(
            method=method, url=_endpoint(path), headers=_headers(),
            json=payload or None, params=params or None, timeout=20,
        )
    except requests.RequestException as exc:
        raise ModerationBackendError(f"Backend request failed: {exc}") from exc

    try:
        body = response.json()
    except ValueError:
        body = {"message": response.text[:300]}

    if response.status_code >= 400:
        message = (
            body.get("message") or body.get("dev_msg")
            or response.text[:300] or "Backend request failed."
        )
        raise ModerationBackendError(message)
    return body


# --- reads ------------------------------------------------------------------

def fetch_reports(status: str = "", content_kind: str = "", overdue: bool = False,
                  limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    params: Dict[str, Any] = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status
    if content_kind:
        params["content_kind"] = content_kind
    if overdue:
        params["overdue"] = "true"
    return _request("GET", "/api/v1/moderation/admin/reports/", params=params)


def fetch_report(report_id: str) -> Dict[str, Any]:
    return _request("GET", f"/api/v1/moderation/admin/reports/{report_id}/")


def fetch_stats() -> Dict[str, Any]:
    return _request("GET", "/api/v1/moderation/admin/stats/")


def fetch_settings() -> Dict[str, Any]:
    """Current enforcement mode (automatic vs manual)."""
    return _request("GET", "/api/v1/moderation/admin/settings/")


def update_settings(enforcement_mode: str = "", auto_min_confidence: Optional[float] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    if enforcement_mode:
        payload["enforcement_mode"] = enforcement_mode
    if auto_min_confidence is not None:
        payload["auto_min_confidence"] = auto_min_confidence
    return _request("POST", "/api/v1/moderation/admin/settings/", payload=payload)


# --- actions ----------------------------------------------------------------

VALID_ACTIONS = {
    # the two escalation decisions (brief A3.4)
    "eject", "keep",
    # finer-grained / reversal actions
    "remove", "ban", "remove_and_ban", "dismiss", "leave_user",
    "restore", "unban", "reanalyze",
}


def action_report(report_id: str, action: str, note: str = "") -> Dict[str, Any]:
    if action not in VALID_ACTIONS:
        raise ModerationBackendError(f"Unknown action '{action}'.")
    return _request(
        "POST", f"/api/v1/moderation/admin/reports/{report_id}/action/",
        payload={"action": action, "note": note},
    )
