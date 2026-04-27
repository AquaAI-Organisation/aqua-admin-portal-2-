"""Shared error classification helpers for OpenAI-backed flows."""
from __future__ import annotations


def classify_openai_error(error_text: str) -> dict[str, str]:
    text = (error_text or "").strip()
    if not text:
        return {
            "category": "",
            "label": "",
            "summary": "",
        }

    lower = text.lower()
    if any(token in lower for token in ("invalid_api_key", "incorrect api key", "401", "authentication")):
        return {
            "category": "auth_error",
            "label": "Auth error",
            "summary": "The configured OpenAI key was rejected during authentication.",
        }
    if any(token in lower for token in ("model", "unsupported", "not found", "does not exist")):
        return {
            "category": "model_config_error",
            "label": "Model/config error",
            "summary": "The configured model or request options are not accepted by the OpenAI API.",
        }
    if any(token in lower for token in ("timeout", "timed out", "connection", "temporarily unavailable", "rate limit", "429", "502", "503", "504")):
        return {
            "category": "transport_error",
            "label": "Transport error",
            "summary": "The OpenAI request could not complete reliably because of a network or service issue.",
        }
    return {
        "category": "scoring_error",
        "label": "Scoring error",
        "summary": "The AI request completed abnormally or returned data the scorer could not use safely.",
    }
