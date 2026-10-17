"""Send published Resend templates using the existing HTTP client and queues."""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import time

import httpx

from .channels import Result

SEVERITY_COLORS = {"info": "#15803d", "warning": "#b54708", "critical": "#b42318"}


def template_variables(payload, location):
    captured = datetime.fromisoformat(payload["ts"].replace("Z", "+00:00"))
    if captured.tzinfo is None:
        raise ValueError("capture timestamp requires a timezone")
    variables = {
        "MESSAGE": payload["message"],
        "SEVERITY": payload["severity"],
        "SEVERITY_COLOR": SEVERITY_COLORS[payload["severity"]],
        "BRANCH_NAME": location,
        "CAMERA_NAME": payload["camera_name"],
        "RULE_NAME": payload["rule_name"],
        "ZONE": payload["zone"],
        "CAPTURED_AT": captured.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "EVENT_REFERENCE": payload["alert_id"],
    }
    if any(not isinstance(v, str) or len(v) > 2000 for v in variables.values()):
        raise ValueError("invalid template variable")
    return variables


class Resend:
    def __init__(self, config, api_key, client, location):
        self.config, self.api_key, self.client, self.location = (
            config,
            api_key,
            client,
            location,
        )

    async def send(self, payload):
        start = time.monotonic()
        cfg = self.config
        try:
            body = {
                "from": cfg.from_address,
                "to": cfg.to,
                "template": {
                    "id": cfg.template_id,
                    "variables": template_variables(payload, self.location),
                },
            }
            identity = [
                payload["alert_id"],
                cfg.name,
                cfg.template_id,
                cfg.from_address,
                sorted(cfg.to),
            ]
            key = hashlib.sha256(
                json.dumps(identity, separators=(",", ":")).encode()
            ).hexdigest()
            async with asyncio.timeout(cfg.timeout_seconds):
                async with self.client.stream(
                    "POST",
                    "https://api.resend.com/emails",
                    json=body,
                    headers={
                        "Authorization": "Bearer " + self.api_key,
                        "Idempotency-Key": key,
                    },
                    timeout=cfg.timeout_seconds,
                    follow_redirects=False,
                ) as response:
                    status = response.status_code
                    success = 200 <= status < 300
                    reason = (
                        "accepted"
                        if success
                        else {
                            401: "authentication_failed",
                            403: "forbidden",
                            422: "invalid_template_or_payload",
                            429: "rate_limited",
                        }.get(status, "http_error")
                    )
                    return Result(
                        success,
                        reason,
                        (time.monotonic() - start) * 1000,
                        status,
                        accepted_recipients=len(cfg.to) if success else 0,
                    )
        except (TimeoutError, httpx.TimeoutException):
            return Result(False, "timeout", (time.monotonic() - start) * 1000)
        except httpx.RequestError:
            return Result(False, "connection_error", (time.monotonic() - start) * 1000)
        except (ValueError, KeyError, TypeError):
            return Result(
                False, "invalid_template_payload", (time.monotonic() - start) * 1000
            )
