"""Runtime configuration with DB overrides on top of environment defaults."""
from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from ..models import OperationalSettings


def _nonempty(value):
    return value not in ("", None)


def get_operational_settings() -> OperationalSettings:
    return OperationalSettings.get_solo()


@dataclass
class EmailRuntimeConfig:
    host: str
    port: int
    use_tls: bool
    username: str
    password: str
    default_from_email: str

    @property
    def configured(self) -> bool:
        return bool(self.host and self.username and self.password)


@dataclass
class SlackRuntimeConfig:
    token: str
    channel: str

    @property
    def configured(self) -> bool:
        return bool(self.token) and "REPLACE" not in self.token.upper()


@dataclass
class MailboxRuntimeConfig:
    host: str
    port: int
    use_ssl: bool
    username: str
    password: str
    folder: str

    @property
    def configured(self) -> bool:
        return bool(self.host and self.username and self.password)


def get_email_runtime_config() -> EmailRuntimeConfig:
    op = get_operational_settings()
    return EmailRuntimeConfig(
        host=op.smtp_host or getattr(settings, "EMAIL_HOST", ""),
        port=op.smtp_port or int(getattr(settings, "EMAIL_PORT", 587)),
        use_tls=op.smtp_use_tls if _nonempty(op.smtp_host) else bool(getattr(settings, "EMAIL_USE_TLS", True)),
        username=op.smtp_username or getattr(settings, "EMAIL_HOST_USER", ""),
        password=op.smtp_password or getattr(settings, "EMAIL_HOST_PASSWORD", ""),
        default_from_email=op.default_from_email or getattr(settings, "DEFAULT_FROM_EMAIL", "Aqua Admin <admin@humara.io>"),
    )


def get_slack_runtime_config() -> SlackRuntimeConfig:
    op = get_operational_settings()
    return SlackRuntimeConfig(
        token=op.slack_bot_token or getattr(settings, "SLACK_BOT_TOKEN", ""),
        channel=op.slack_channel or getattr(settings, "SLACK_CHANNEL", ""),
    )


def get_mailbox_runtime_config() -> MailboxRuntimeConfig:
    op = get_operational_settings()
    return MailboxRuntimeConfig(
        host=op.imap_host,
        port=op.imap_port,
        use_ssl=op.imap_use_ssl,
        username=op.imap_username,
        password=op.imap_password,
        folder=op.imap_folder or "INBOX",
    )
