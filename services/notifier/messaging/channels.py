"""Independent HTTP and SMTP delivery with bounded request deadlines."""

import asyncio
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
import json
import time

import aiosmtplib
import httpx


@dataclass
class Result:
    success: bool
    reason: str
    elapsed_ms: float
    status_code: int | None = None
    accepted_recipients: int = 0
    refused_recipients: int = 0


class Webhook:
    def __init__(self, config, api_key, client):
        self.config, self.api_key, self.client = config, api_key, client

    async def send(self, payload):
        start = time.monotonic()
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                async with self.client.stream(
                    "POST",
                    self.config.url,
                    # Keep the existing backend webhook schema unchanged.
                    json={k: v for k, v in payload.items() if k != "alert_id"},
                    headers={"X-API-Key": self.api_key}
                    if self.config.auth == "api_key"
                    else {},
                    timeout=self.config.timeout_seconds,
                    follow_redirects=False,
                ) as response:
                    status = response.status_code
                    return Result(
                        200 <= status < 300,
                        "accepted" if 200 <= status < 300 else "http_error",
                        (time.monotonic() - start) * 1000,
                        status,
                    )
        except (TimeoutError, httpx.TimeoutException):
            return Result(False, "timeout", (time.monotonic() - start) * 1000)
        except httpx.RequestError:
            return Result(False, "connection_error", (time.monotonic() - start) * 1000)


def email_message(config, payload):
    message = EmailMessage()
    message["From"] = config.from_address
    message["To"] = ", ".join(config.to)
    # Alert fields are untrusted content; subject stays a bounded single line.
    subject = f"{config.subject_prefix} {payload['severity'].upper()} {payload['rule_name']} - {payload['camera_name']}"
    message["Subject"] = " ".join(subject.split())[:240]
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid()
    message.set_content(
        f"{payload['message']}\n\nRule: {payload['rule_name']}\nSeverity: {payload['severity']}\nCamera: {payload['camera_name']} ({payload['camera_id']})\nZone: {payload['zone']}\nCaptured: {payload['ts']}\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    return message


class Email:
    def __init__(self, config, password=None):
        self.config, self.password = config, password

    async def send(self, payload):
        start = time.monotonic()
        cfg = self.config
        client = aiosmtplib.SMTP(
            hostname=cfg.smtp.host,
            port=cfg.smtp.port,
            username=cfg.smtp.username,
            password=self.password,
            use_tls=cfg.smtp.security == "ssl",
            start_tls=cfg.smtp.security == "starttls",
            validate_certs=True,
            timeout=cfg.timeout_seconds,
            local_hostname="visionengine.local",
        )
        accepted = None
        try:
            async with asyncio.timeout(cfg.timeout_seconds):
                await client.connect()
                refused, _ = await client.send_message(
                    email_message(cfg, payload),
                    sender=parseaddr(cfg.from_address)[1],
                    recipients=cfg.to,
                )
                accepted = Result(
                    not refused,
                    "partial_rejection" if refused else "accepted",
                    0,
                    accepted_recipients=len(cfg.to) - len(refused),
                    refused_recipients=len(refused),
                )
                try:
                    await client.quit()
                except (aiosmtplib.SMTPException, OSError):
                    pass
            accepted.elapsed_ms = (time.monotonic() - start) * 1000
            return accepted
        except (TimeoutError, aiosmtplib.SMTPException, OSError, ValueError):
            if accepted is not None:
                accepted.elapsed_ms = (time.monotonic() - start) * 1000
                return accepted  # DATA accepted before a QUIT timeout; do not miscount it as undelivered.
            return Result(
                False, "smtp_delivery_failed", (time.monotonic() - start) * 1000
            )
        finally:
            client.close()
