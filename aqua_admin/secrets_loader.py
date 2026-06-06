"""Central secrets manager client — fetches this service's secrets at startup.

OPT-IN: does nothing unless SECRETS_SERVICE_TOKEN is set, so the app keeps using its
existing environment until you switch it on. When the token is present it calls the
Supabase `secrets-fetch` edge function and injects the returned values into the
process environment BEFORE the app reads them. FAILS CLOSED (raises) if the token is
set but the fetch fails — the app never starts with missing/partial secrets.

Set these on the host (Heroku Config Vars) to enable:
  SECRETS_SERVICE_TOKEN       this service's token (from secretsmgr_issue_token)
  SECRETS_FETCH_URL           https://<ref>.supabase.co/functions/v1/secrets-fetch
  SECRETS_SUPABASE_ANON_KEY   the project's public anon key (gateway apikey only)
"""
from __future__ import annotations

import json
import os
import urllib.request

_DEFAULT_URL = "https://kfcnaeotwzfnluxkmefj.supabase.co/functions/v1/secrets-fetch"


def load() -> None:
    token = os.environ.get("SECRETS_SERVICE_TOKEN")
    if not token:
        return  # not enabled on this host yet — keep existing env behaviour

    url = os.environ.get("SECRETS_FETCH_URL", _DEFAULT_URL)
    apikey = os.environ.get("SECRETS_SUPABASE_ANON_KEY", "")
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "apikey": apikey,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # fail closed — do not start without secrets
        raise RuntimeError(f"secrets-manager fetch failed: {exc}") from exc

    if not isinstance(payload, dict) or "error" in payload:
        raise RuntimeError(f"secrets-manager returned an error: {payload}")

    for key, value in payload.items():
        if isinstance(value, str):
            os.environ[key] = value
