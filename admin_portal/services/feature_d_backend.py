from __future__ import annotations

from typing import Any, Dict, Optional

import requests
from django.conf import settings


class FeatureDBackendError(Exception):
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


def _request(method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not is_configured():
        raise FeatureDBackendError(
            "Backend admin bridge is not configured. Set AQUAAI_BACKEND_API_URL and AQUAAI_BACKEND_API_TOKEN."
        )
    try:
        response = requests.request(
            method=method,
            url=_endpoint(path),
            headers=_headers(),
            json=payload or None,
            timeout=15,
        )
    except requests.RequestException as exc:
        raise FeatureDBackendError(f"Backend request failed: {exc}") from exc

    try:
        body = response.json()
    except ValueError:
        body = {"message": response.text[:300]}

    if response.status_code >= 400:
        message = body.get("message") or body.get("dev_msg") or response.text[:300] or "Backend request failed."
        raise FeatureDBackendError(message)
    return body


def fetch_feature_d_dashboard() -> Dict[str, Any]:
    return _request("GET", "/api/v1/marketplace/admin/reservations/dashboard/")


def approve_verification(verification_id: int) -> Dict[str, Any]:
    return _request(
        "POST",
        f"/api/v1/marketplace/admin/verifications/{verification_id}/review/",
        {"decision": "approve"},
    )


def reject_verification(verification_id: int, rejection_reason: str) -> Dict[str, Any]:
    return _request(
        "POST",
        f"/api/v1/marketplace/admin/verifications/{verification_id}/review/",
        {"decision": "reject", "rejection_reason": rejection_reason},
    )


def resolve_dispute(dispute_id: int, resolution: str, summary: str) -> Dict[str, Any]:
    return _request(
        "POST",
        f"/api/v1/marketplace/admin/disputes/{dispute_id}/resolve/",
        {"resolution": resolution, "summary": summary},
    )


def toggle_delivery(seller_id: str, enabled: bool) -> Dict[str, Any]:
    return _request(
        "POST",
        f"/api/v1/marketplace/admin/breeders/{seller_id}/delivery-toggle/",
        {"enabled": enabled},
    )
