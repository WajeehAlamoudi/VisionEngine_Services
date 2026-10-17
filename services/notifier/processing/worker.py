"""Bounded fan-out: one independent asynchronous delivery worker per destination."""

import asyncio
from collections import OrderedDict
import json
import logging

from .queue import Inbox
from .payload import build_payload
from ..messaging.channels import Webhook, Email
from ..messaging.resend import Resend

log = logging.getLogger("notifier")


class Dispatcher:
    def __init__(
        self,
        settings,
        api,
        device,
        client,
        passwords,
        *,
        max_items=256,
        max_bytes=4194304,
        max_age_ms=30000,
    ):
        self.settings, self.api, self.device = settings, api, device
        self.channels = {}
        for kind, configs in (
            ("webhook", settings.webhooks),
            ("email", settings.emails),
        ):
            for cfg in configs:
                if cfg.enabled:
                    if kind == "webhook":
                        sender = Webhook(cfg, api.key, client)
                    elif cfg.provider == "resend":
                        sender = Resend(
                            cfg, passwords.get(cfg.name), client, device.location
                        )
                    else:
                        sender = Email(cfg, passwords.get(cfg.name))
                    self.channels[cfg.name] = {
                        "kind": kind,
                        "config": cfg,
                        "sender": sender,
                        "inbox": Inbox(max_items, max_bytes, max_age_ms),
                        "sent_total": 0,
                        "failed_total": 0,
                        "inflight": 0,
                        "cancelled_total": 0,
                        "last_ms": 0,
                        "last_status": None,
                        "last_error": None,
                        "accepted_recipients_total": 0,
                        "refused_recipients_total": 0,
                    }
        self.processed = self.alerts_total = self.logged_total = (
            self.duplicate_batches
        ) = 0
        self.seen = OrderedDict()

    def submit(self, batch):
        key = (batch["camera_id"], batch["stream_id"], batch["sequence"])
        if key in self.seen:
            self.duplicate_batches += 1
            return
        self.seen[key] = True
        if len(self.seen) > 4096:
            self.seen.popitem(last=False)
        self.processed += 1
        for alert_index, alert in enumerate(batch["alerts"]):
            self.alerts_total += 1
            if self.settings.log.enabled:
                det = alert["detection"]
                log.info(
                    "rule alert",
                    extra={
                        "stage": "output",
                        "event": "alert_logged",
                        "fields": {
                            "rule": alert["rule_name"],
                            "severity": alert["severity"],
                            "camera_id": det["camera_id"],
                            "zone": det["zone"],
                            "class": det["class_name"],
                            "confidence": round(det["confidence"], 4),
                            "track_id": det["track_id"],
                        },
                    },
                )
                self.logged_total += 1
            payload = build_payload(
                alert,
                self.device.id,
                self.api.branch_id,
                source=(batch["stream_id"], batch["sequence"], alert_index),
            )
            size = len(
                json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
            )
            for channel in self.channels.values():
                if channel["config"].accepts(alert["rule_name"], alert["severity"]):
                    channel["inbox"].put(payload, size)

    async def deliver(self, channel, closing):
        while not closing.is_set() or channel["inbox"].items:
            payload = channel["inbox"].pop()
            if payload is None:
                await asyncio.sleep(0.02)
                continue
            channel["inflight"] = 1
            try:
                result = await channel["sender"].send(payload)
                channel["last_ms"] = result.elapsed_ms
                channel["last_status"] = result.status_code
                channel["last_error"] = None if result.success else result.reason
                channel["sent_total" if result.success else "failed_total"] += 1
                channel["accepted_recipients_total"] += result.accepted_recipients
                channel["refused_recipients_total"] += result.refused_recipients
                logger = log.info if result.success else log.warning
                logger(
                    "notification delivered"
                    if result.success
                    else "notification delivery failed; not retried",
                    extra={
                        "stage": "output",
                        "event": "delivery_result",
                        "fields": {
                            "channel": channel["config"].name,
                            "kind": channel["kind"],
                            "camera_id": payload["camera_id"],
                            "rule": payload["rule_name"],
                            "result": result.reason,
                            "http_status": result.status_code,
                            "delivery_ms": round(result.elapsed_ms, 2),
                            "accepted_recipients": result.accepted_recipients,
                            "refused_recipients": result.refused_recipients,
                        },
                    },
                )
            except asyncio.CancelledError:
                channel["cancelled_total"] += 1
                raise
            except Exception as exc:
                channel["failed_total"] += 1
                channel["last_error"] = "channel_error"
                log.error(
                    "notification channel failed (%s)",
                    type(exc).__name__,
                    extra={"fields": {"channel": channel["config"].name}},
                )
            finally:
                channel["inflight"] = 0

    def snapshot(self):
        channels = {
            name: {
                **c["inbox"].snapshot(),
                "kind": c["kind"],
                **{
                    k: c[k]
                    for k in (
                        "sent_total",
                        "failed_total",
                        "inflight",
                        "cancelled_total",
                        "last_ms",
                        "last_status",
                        "last_error",
                        "accepted_recipients_total",
                        "refused_recipients_total",
                    )
                },
            }
            for name, c in self.channels.items()
        }
        return {
            "processed": self.processed,
            "alerts_total": self.alerts_total,
            "logged_total": self.logged_total,
            "duplicate_batches": self.duplicate_batches,
            "sent_total": sum(c["sent_total"] for c in channels.values()),
            "dependency_error": "notification_delivery_failure"
            if any(c["last_error"] for c in channels.values())
            else None,
            "channels": channels,
        }
