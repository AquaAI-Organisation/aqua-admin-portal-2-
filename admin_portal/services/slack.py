"""Slack integration — typed service client for the admin control plane.

Applies the same integration strategy the command-centre uses for its other
connected systems (a typed client lib with an `isConfigured()` gate, a live
`status` check against the provider, and graceful degradation):

  * slack_configured()  — is a usable bot token present?
  * slack_status()      — live status via Slack `auth.test` (team / bot / channel),
                          mirroring the reference integration's `status` endpoint.
  * send_message()      — single-message primitive; returns {ok, error}; never raises.
  * send_to_super_admins() — DM each super-admin + post to the fallback channel.
  * send_test_message() — used by the Settings "Test" button.

Credentials come from OperationalSettings (admin self-service) with env fallback,
via runtime_config.get_slack_runtime_config(). Nothing here ever raises into the
caller — if Slack is not configured or is unreachable, calls no-op and log.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

from .runtime_config import get_slack_runtime_config

logger = logging.getLogger(__name__)


def slack_configured() -> bool:
    return get_slack_runtime_config().configured


@lru_cache(maxsize=2)
def _client_for(token: str):
    from slack_sdk import WebClient

    return WebClient(token=token)


def _get_client():
    cfg = get_slack_runtime_config()
    if not cfg.configured:
        return None
    return _client_for(cfg.token)


def clear_slack_client_cache() -> None:
    """Call after the token changes so a stale client is not reused."""
    _client_for.cache_clear()


def slack_status() -> dict:
    """Live connection status — {configured, connected, note, team, bot_user, channel}."""
    cfg = get_slack_runtime_config()
    if not cfg.configured:
        return {
            "configured": False,
            "connected": False,
            "channel": cfg.channel,
            "note": "Add a Slack bot token (and optional fallback channel) to enable Slack alerts.",
        }
    try:
        from slack_sdk.errors import SlackApiError

        client = _get_client()
        resp = client.auth_test()
        return {
            "configured": True,
            "connected": True,
            "team": resp.get("team"),
            "bot_user": resp.get("user"),
            "channel": cfg.channel,
            "note": f"Connected to {resp.get('team')} as {resp.get('user')}.",
        }
    except SlackApiError as exc:
        err = exc.response.get("error", "invalid_auth") if getattr(exc, "response", None) else "invalid_auth"
        return {
            "configured": True,
            "connected": False,
            "channel": cfg.channel,
            "note": f"Slack rejected the token ({err}). Re-check the bot token in Settings.",
        }
    except Exception:
        logger.exception("Slack auth_test failed")
        return {
            "configured": True,
            "connected": False,
            "channel": cfg.channel,
            "note": "Could not reach Slack to verify the token. Check the network or token.",
        }


def send_message(text: str, channel: Optional[str] = None, blocks: Optional[list] = None) -> dict:
    """Send one message to a channel (or the configured fallback). Returns {ok, error}."""
    cfg = get_slack_runtime_config()
    if not cfg.configured:
        logger.warning("Slack not configured; skipping message")
        return {"ok": False, "error": "Slack is not configured."}
    target = channel or cfg.channel
    if not target:
        return {"ok": False, "error": "No Slack channel given and no fallback channel configured."}
    try:
        client = _get_client()
        client.chat_postMessage(channel=target, text=text, blocks=blocks)
        return {"ok": True, "error": ""}
    except Exception as exc:
        logger.exception("Slack send failed")
        return {"ok": False, "error": str(exc)}


def send_to_super_admins(text: str) -> bool:
    """DM each super-admin (looked up by email) and post to the fallback channel.
    Best-effort and graceful; returns True if at least one delivery succeeded."""
    cfg = get_slack_runtime_config()
    if not cfg.configured:
        logger.warning("Slack not configured; skipping super-admin notification")
        return False
    try:
        from django.conf import settings as dj_settings
        from slack_sdk.errors import SlackApiError

        client = _get_client()
        delivered = False
        for email in getattr(dj_settings, "SUPERADMIN_EMAILS", []) or []:
            try:
                lookup = client.users_lookupByEmail(email=email)
                dm = client.conversations_open(users=lookup["user"]["id"])
                client.chat_postMessage(channel=dm["channel"]["id"], text=text)
                delivered = True
            except SlackApiError:
                logger.exception("Slack DM lookup failed for %s", email)
            except Exception:
                logger.exception("Slack DM send failed for %s", email)
        if cfg.channel:
            client.chat_postMessage(channel=cfg.channel, text=text)
            delivered = True
        return delivered
    except Exception:
        logger.exception("Slack client error")
        return False


def send_test_message(actor_email: Optional[str] = None) -> dict:
    who = f" (triggered by {actor_email})" if actor_email else ""
    return send_message(
        f":white_check_mark: Aqua Admin Slack test{who} — your Slack integration is working."
    )
