"""Resolve OpenAI runtime configuration from Supabase edge functions first.

Falls back to Django settings/env when the edge function is not configured or
temporarily unavailable. This keeps the admin portal aligned with the wider
AquaAI architecture where secrets may be brokered via Supabase.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from urllib import error, request

from django.conf import settings


_CACHE: dict[str, object] = {
    "checked_at": 0.0,
    "config": None,
}


@dataclass
class OpenAIRuntimeConfig:
    key: str
    model: str
    source: str
    error: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.key)


def _strip(value) -> str:
    return str(value or "").strip()


def _edge_url() -> str:
    direct = _strip(getattr(settings, "OPENAI_EDGE_FUNCTION_URL", ""))
    if direct:
        return direct
    supabase_url = _strip(getattr(settings, "SUPABASE_URL", ""))
    fn_name = _strip(getattr(settings, "OPENAI_EDGE_FUNCTION_NAME", ""))
    if supabase_url and fn_name:
        return f"{supabase_url.rstrip('/')}/functions/v1/{fn_name.lstrip('/')}"
    return ""


def _extract_payload(payload: object) -> tuple[str, str]:
    if isinstance(payload, str):
        key = payload.strip()
        return key, ""
    if not isinstance(payload, dict):
        return "", ""

    candidates = [
        payload,
        payload.get("data") if isinstance(payload.get("data"), dict) else None,
        payload.get("config") if isinstance(payload.get("config"), dict) else None,
        payload.get("result") if isinstance(payload.get("result"), dict) else None,
    ]
    for block in candidates:
        if not isinstance(block, dict):
            continue
        key = _strip(
            block.get("openai_api_key")
            or block.get("OPENAI_API_KEY")
            or block.get("api_key")
            or block.get("key")
            or block.get("full_key")
            or block.get("secret")
        )
        model = _strip(
            block.get("openai_model")
            or block.get("OPENAI_MODEL")
            or block.get("model")
        )
        if key:
            return key, model
    return "", ""


def _fetch_from_edge() -> OpenAIRuntimeConfig | None:
    url = _edge_url()
    service_key = _strip(getattr(settings, "SUPABASE_SERVICE_KEY", ""))
    if not url:
        return None

    headers = {"Content-Type": "application/json"}
    if service_key:
        headers["Authorization"] = f"Bearer {service_key}"
        headers["apikey"] = service_key
    errors: list[str] = []
    attempts = [
        ("GET", None),
        ("POST", b"{}"),
    ]
    for method, body in attempts:
        req = request.Request(url, data=body, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=12) as response:
                content = response.read().decode("utf-8", errors="replace").strip()
                if not content:
                    continue
                try:
                    payload = json.loads(content)
                except json.JSONDecodeError:
                    payload = content
                key, model = _extract_payload(payload)
                if key:
                    return OpenAIRuntimeConfig(
                        key=key,
                        model=model or _strip(getattr(settings, "OPENAI_MODEL", "gpt-4o")),
                        source="supabase_edge_function",
                    )
                errors.append(f"{method} returned no usable OpenAI key payload.")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            errors.append(f"{method} {exc.code}: {detail or exc.reason}")
        except Exception as exc:  # pragma: no cover - network/runtime dependent
            errors.append(f"{method} failed: {exc}")
    return OpenAIRuntimeConfig(
        key="",
        model=_strip(getattr(settings, "OPENAI_MODEL", "gpt-4o")),
        source="supabase_edge_function",
        error=" | ".join(errors),
    )


def get_openai_runtime_config(*, force_refresh: bool = False) -> OpenAIRuntimeConfig:
    now = time.monotonic()
    cached = _CACHE.get("config")
    if (
        not force_refresh
        and isinstance(cached, OpenAIRuntimeConfig)
        and (now - float(_CACHE.get("checked_at", 0.0))) < 300
    ):
        return cached

    edge_config = _fetch_from_edge()
    if edge_config and edge_config.configured:
        _CACHE.update({"checked_at": now, "config": edge_config})
        return edge_config

    fallback = OpenAIRuntimeConfig(
        key=_strip(getattr(settings, "OPENAI_API_KEY", "")),
        model=_strip(getattr(settings, "OPENAI_MODEL", "gpt-4o")),
        source="django_settings_env",
        error=edge_config.error if edge_config else "",
    )
    _CACHE.update({"checked_at": now, "config": fallback})
    return fallback
