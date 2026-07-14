"""Optional secrets bootstrap.

Pulls secrets from AWS Secrets Manager into the process environment BEFORE Django
settings read them — but only when AWS is actually configured, and NEVER at the cost
of the app booting.

Design: opt-in + fail-open.
- If no AWS credentials are present, this is a no-op and the app uses its existing
  ``.env`` / host environment (the normal production path today).
- If the fetch fails for any reason (missing boto3, bad token, network, missing
  secret), it logs a warning and continues rather than taking the whole control
  plane offline. A hard failure here would 500 every request, including login.
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

SECRET_NAME = os.getenv("AWS_SECRET_NAME", "aqua_backend")
REGION = os.getenv("AWS_REGION", "eu-west-2")


def load_aws_secrets() -> dict:
    # Only attempt AWS when explicitly configured with credentials.
    if not (os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY")):
        return {}
    try:
        import boto3  # imported lazily so a missing dependency can't break startup

        client = boto3.client(
            "secretsmanager",
            region_name=REGION,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )
        response = client.get_secret_value(SecretId=SECRET_NAME)
        secrets = json.loads(response["SecretString"])
    except Exception:
        logger.warning(
            "AWS Secrets Manager load skipped/failed; falling back to existing environment.",
            exc_info=True,
        )
        return {}

    for key, value in secrets.items():
        os.environ[key] = str(value)
    return secrets


# Backwards-compatible alias: earlier code (and any external callers) used load().
def load() -> dict:
    return load_aws_secrets()
