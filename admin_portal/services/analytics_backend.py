"""Bridge to the AquaAI backend's admin analytics API.

Mirrors feature_d_backend.py / moderation_backend.py: same configuration, auth
header and error handling.

Note this deliberately does NOT go through services/bi.py. That engine reads the
backend tables directly through unmanaged mirror models and drives the existing
Reports section, CSV and PDF exports. Analytics here comes from the backend's
own services instead, so the figures an admin sees are computed by exactly the
same code the user or provider sees — and bi.py is left untouched.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import requests
from django.conf import settings


class AnalyticsBackendError(Exception):
    pass


ENTITY_TYPES = ("users", "consultants", "breeders")
TYPE_LABELS = {
    "users": "Users",
    "consultants": "Consultants",
    "breeders": "Breeders",
}
PERIODS = [("1m", "Last month"), ("3m", "Last 3 months"), ("6m", "Last 6 months"),
           ("12m", "Last 12 months"), ("all", "All time")]


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
    return f"{settings.AQUAAI_BACKEND_API_URL.rstrip('/')}{path}"


def _request(method: str, path: str, params: Optional[Dict[str, Any]] = None,
             payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not is_configured():
        raise AnalyticsBackendError(
            "Backend bridge is not configured. Set AQUAAI_BACKEND_API_URL and "
            "AQUAAI_BACKEND_API_TOKEN."
        )
    try:
        response = requests.request(
            method=method, url=_endpoint(path), headers=_headers(),
            params=params or None, json=payload or None, timeout=30,
        )
    except requests.RequestException as exc:
        raise AnalyticsBackendError(f"Backend request failed: {exc}") from exc

    try:
        body = response.json()
    except ValueError:
        body = {"message": response.text[:300]}

    if response.status_code >= 400:
        raise AnalyticsBackendError(
            body.get("message") or body.get("dev_msg")
            or response.text[:300] or "Backend request failed."
        )
    return body


def _period_params(period: str = "", start: str = "", end: str = "") -> Dict[str, Any]:
    if start or end:
        return {"start_date": start, "end_date": end}
    return {"period": period or "1m"}


def fetch_overview(entity_type: str, period: str = "", start: str = "",
                   end: str = "") -> Dict[str, Any]:
    """Platform-wide analytics for one app type."""
    params = {"type": entity_type, **_period_params(period, start, end)}
    return _request("GET", "/api/v1/user-analytics/admin/overview/", params=params)


def search_entities(entity_type: str, query: str = "", limit: int = 25) -> Dict[str, Any]:
    """Find an account by email, name or id."""
    return _request("GET", "/api/v1/user-analytics/admin/search/",
                    params={"type": entity_type, "q": query, "limit": limit})


def fetch_entity(entity_type: str, entity_id: str, period: str = "",
                 start: str = "", end: str = "") -> Dict[str, Any]:
    """One named account's analytics."""
    return _request(
        "GET", f"/api/v1/user-analytics/admin/entity/{entity_type}/{entity_id}/",
        params=_period_params(period, start, end),
    )
