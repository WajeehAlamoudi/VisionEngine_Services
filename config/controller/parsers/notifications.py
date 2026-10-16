from __future__ import annotations

from dataclasses import dataclass, field
from email.headerregistry import Address
from email.errors import HeaderParseError
import re
from typing import Any

from .rule import SEVERITIES
from .strict import ALL, ID_PATTERN, Reader, describe

_FILE = "notifications.yaml"

_TOP_KEYS = ("log", "webhooks", "emails")
_LOG_KEYS = ("enabled",)
_WEBHOOK_KEYS = (
    "name",
    "url",
    "enabled",
    "timeout_seconds",
    "rules",
    "severities",
    "auth",
)


@dataclass
class LogChannelConfig:
    enabled: bool


@dataclass
class WebhookConfig:
    name: str
    url: str
    enabled: bool
    timeout_seconds: int
    # [] = every rule / every severity, resolved from "*" at parse time.
    # Both filters must pass for a webhook to fire on a given match.
    rules: list[str]
    severities: list[str]
    auth: str = "api_key"

    def accepts(self, rule_name: str, severity: str) -> bool:
        """Whether this webhook should fire for a match from the given rule."""
        if self.rules and rule_name not in self.rules:
            return False
        if self.severities and severity not in self.severities:
            return False
        return True


@dataclass
class SmtpConfig:
    host: str
    port: int
    security: str
    username: str | None
    password_env: str | None


@dataclass
class EmailConfig:
    name: str
    enabled: bool
    smtp: SmtpConfig
    from_address: str
    to: list[str]
    subject_prefix: str
    timeout_seconds: int
    rules: list[str]
    severities: list[str]

    def accepts(self, rule_name: str, severity: str) -> bool:
        return (not self.rules or rule_name in self.rules) and (
            not self.severities or severity in self.severities
        )


@dataclass
class NotificationsConfig:
    log: LogChannelConfig
    webhooks: list[WebhookConfig]
    emails: list[EmailConfig] = field(default_factory=list)


def _parse_webhook(r: Reader) -> WebhookConfig:
    r.reject_unknown(*_WEBHOOK_KEYS)

    name = r.identifier(
        "name",
        ID_PATTERN,
        "letters, digits, hyphens, and underscores only (identifies this webhook in logs)",
    )
    url = r.url("url")
    enabled = r.boolean("enabled")
    timeout_seconds = r.integer("timeout_seconds", minimum=1)

    rules = r.string_list_or_all("rules")
    severities = r.string_list_or_all("severities")

    if severities != ALL:
        for i, severity in enumerate(severities):
            if severity not in SEVERITIES:
                r.error(
                    f"{r.path_of('severities')}[{i}]",
                    f"expected one of {' | '.join(SEVERITIES)}, got '{severity}'",
                )

    auth = r.raw.get("auth", "api_key")
    if auth not in ("api_key", "none"):
        r.error(r.path_of("auth"), "expected api_key or none")
    return WebhookConfig(
        name=name,
        auth=auth,
        url=url,
        enabled=enabled,
        timeout_seconds=timeout_seconds,
        rules=[] if rules == ALL else list(rules),
        severities=[] if severities == ALL else list(severities),
    )


def _mailbox(r, key):
    value = r.string(key)
    try:
        value.encode("ascii")
        address = Address(addr_spec=value)
        if (
            not address.username
            or not address.domain
            or any(c.isspace() for c in value)
        ):
            raise ValueError("invalid address")
    except (ValueError, TypeError, UnicodeError, HeaderParseError, IndexError):
        r.error(
            r.path_of(key),
            "expected a single ASCII email address without display name or newlines",
        )
    return value


def _parse_email(r):
    r.reject_unknown(
        "name",
        "enabled",
        "smtp",
        "from_address",
        "to",
        "subject_prefix",
        "timeout_seconds",
        "rules",
        "severities",
    )
    name = r.identifier(
        "name", ID_PATTERN, "letters, digits, hyphens, underscores only"
    )
    enabled = r.boolean("enabled")
    smtp = r.section("smtp")
    smtp.reject_unknown("host", "port", "security", "username", "password_env")
    host = smtp.string("host")
    if len(host) > 253 or any(c.isspace() for c in host) or "://" in host:
        smtp.error(
            smtp.path_of("host"),
            "expected SMTP hostname or IP without URL scheme or whitespace",
        )
    port = smtp.integer("port", minimum=1, maximum=65535)
    security = smtp.string("security")
    if security not in ("starttls", "ssl", "none"):
        smtp.error(smtp.path_of("security"), "expected starttls, ssl or none")
    username = smtp.string_or_null("username") if smtp.has("username") else None
    password_env = (
        smtp.string_or_null("password_env") if smtp.has("password_env") else None
    )
    if bool(username) != bool(password_env):
        smtp.error(
            smtp.path_of("username"),
            "username and password_env must both be set or both be null",
        )
    if password_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", password_env):
        smtp.error(
            smtp.path_of("password_env"), "expected an environment variable name"
        )
    if username and security == "none":
        smtp.error(
            smtp.path_of("security"), "SMTP authentication requires starttls or ssl"
        )
    if username and any(c in username for c in "\r\n"):
        smtp.error(smtp.path_of("username"), "newlines are not allowed")
    sender = _mailbox(r, "from_address")
    recipients = r.raw.get("to")
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= 50:
        r.error(r.path_of("to"), "expected 1..50 recipient email addresses")
        recipients = []
    to = []
    for i, value in enumerate(recipients):
        recipient = _mailbox(
            r.child(f"{r.path_of('to')}[{i}]", {"address": value}), "address"
        )
        if recipient in to:
            r.error(r.path_of("to"), "duplicate recipient")
        to.append(recipient)
    prefix = r.string("subject_prefix")
    if len(prefix) > 120 or any(c in prefix for c in "\r\n"):
        r.error(
            r.path_of("subject_prefix"),
            "expected a single line of at most 120 characters",
        )
    timeout = r.integer("timeout_seconds", minimum=1, maximum=300)
    rules = r.string_list_or_all("rules")
    severities = r.string_list_or_all("severities")
    if severities != ALL and any(s not in SEVERITIES for s in severities):
        r.error(r.path_of("severities"), "expected info, warning or critical")
    return EmailConfig(
        name,
        enabled,
        SmtpConfig(host, port, security, username, password_env),
        sender,
        to,
        prefix,
        timeout,
        [] if rules == ALL else list(rules),
        [] if severities == ALL else list(severities),
    )


def parse(raw: Any) -> NotificationsConfig:
    """
    Parse and validate notifications.yaml.

    An empty `webhooks:` list is allowed here, unlike the empty lists rejected
    elsewhere. Everywhere else an empty list is a filter that would be
    ambiguous with "all", or a collection whose emptiness breaks the device.
    Zero webhooks is neither: it is a working configuration that delivers
    alerts to the log only.

    Whether a webhook may name a rule that does not exist is checked in
    _validate, where rules.yaml is in scope.
    """
    r = Reader(_FILE, raw)
    r.reject_unknown(*_TOP_KEYS)

    log_r = r.section("log")
    log_r.reject_unknown(*_LOG_KEYS)
    log_cfg = LogChannelConfig(enabled=log_r.boolean("enabled"))

    raw_webhooks = r.raw.get("webhooks", None)
    if "webhooks" not in r.raw:
        r.error(
            "webhooks", "required key is missing (use an empty list for no webhooks)"
        )
        raw_webhooks = []
    elif not isinstance(raw_webhooks, list):
        r.error(
            "webhooks", f"expected a list of webhooks, got {describe(raw_webhooks)}"
        )
        raw_webhooks = []

    webhooks: list[WebhookConfig] = []
    seen: set[str] = set()

    for i, item in enumerate(raw_webhooks):
        path = f"webhooks[{i}]"
        webhook = _parse_webhook(r.child(path, item))

        if webhook.name:
            if webhook.name in seen:
                r.error(f"{path}.name", f"duplicate webhook name '{webhook.name}'")
            seen.add(webhook.name)

        webhooks.append(webhook)

    raw_emails = r.raw.get("emails", [])
    if not isinstance(raw_emails, list):
        r.error("emails", "expected a list of email channels")
        raw_emails = []
    if len(raw_emails) + len(raw_webhooks) > 64:
        r.error("emails", "at most 64 delivery channels are supported")
    emails = []
    for i, item in enumerate(raw_emails):
        email = _parse_email(r.child(f"emails[{i}]", item))
        if email.name in seen:
            r.error(
                f"emails[{i}].name",
                "channel names must be unique across webhooks and emails",
            )
        seen.add(email.name)
        emails.append(email)
    r.raise_if_errors()
    return NotificationsConfig(log=log_cfg, webhooks=webhooks, emails=emails)
