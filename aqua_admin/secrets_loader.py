"""Secrets bootstrap — pull the up-to-date credentials from AWS Secrets Manager
into the process environment BEFORE Django settings read them.

AWS Secrets Manager is the source of truth for rotated keys, so this runs on every
startup when AWS is configured. It supports both auth styles:
  - explicit AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in the environment, or
  - the default boto3 credential chain (e.g. an IAM instance/task role) when no
    explicit keys are set.

Fail-open by default: if the fetch cannot run (boto3 missing, no creds, network,
missing secret) it logs a LOUD warning and falls back to the existing environment
rather than 500-ing every request — a hard failure here takes the whole control
plane offline, including login. Set AWS_SECRETS_REQUIRED=true to fail closed
instead (refuse to boot unless the secret loads), if you prefer never running on
stale/fallback values.
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

SECRET_NAME = os.getenv("AWS_SECRET_NAME", "aqua_backend")
REGION = os.getenv("AWS_REGION", "eu-west-2")


def _aws_configured() -> bool:
    # Consider AWS "in use" when creds, an explicit secret name, or a region are set.
    return bool(
        os.getenv("AWS_ACCESS_KEY_ID")
        or os.getenv("AWS_SECRET_NAME")
        or os.getenv("AWS_DEFAULT_REGION")
        or os.getenv("AWS_REGION")
    )


def load_aws_secrets() -> dict:
    if not _aws_configured():
        return {}

    required = os.getenv("AWS_SECRETS_REQUIRED", "false").lower() in ("1", "true", "yes")
    try:
        import boto3  # lazy import so a missing dependency can't crash startup

        client_kwargs = {"region_name": REGION}
        # Only pass explicit keys if present; otherwise let boto3 use its default
        # chain (IAM role, shared config, etc.).
        if os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"):
            client_kwargs["aws_access_key_id"] = os.getenv("AWS_ACCESS_KEY_ID")
            client_kwargs["aws_secret_access_key"] = os.getenv("AWS_SECRET_ACCESS_KEY")

        client = boto3.client("secretsmanager", **client_kwargs)
        response = client.get_secret_value(SecretId=SECRET_NAME)
        secrets = json.loads(response["SecretString"])
    except Exception as exc:
        if required:
            # Opt-in strict mode: do not boot on stale/fallback values.
            raise RuntimeError(
                f"AWS Secrets Manager load failed and AWS_SECRETS_REQUIRED is set: {exc}"
            ) from exc
        logger.warning(
            "AWS Secrets Manager load failed; falling back to the existing environment. "
            "Rotated keys will NOT be picked up until this succeeds.",
            exc_info=True,
        )
        return {}

    for key, value in secrets.items():
        os.environ[key] = str(value)
    logger.info("Loaded %d secret(s) from AWS Secrets Manager (%s).", len(secrets), SECRET_NAME)
    return secrets


# Backwards-compatible alias for earlier callers that used load().
def load() -> dict:
    return load_aws_secrets()
