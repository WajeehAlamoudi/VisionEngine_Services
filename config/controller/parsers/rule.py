from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .strict import (
    ALL,
    ID_PATTERN,
    TABLE_NAME_PATTERN,
    TIME_PATTERN,
    Reader,
    describe,
)

_FILE = "rules.yaml"

_RULE_KEYS = (
    "name",
    "class",
    "cameras",
    "zones",
    "min_confidence",
    "cooldown_seconds",
    "notify",
    "severity",
    "notifications_table",
    "message",
    "enabled",
    "schedule",
)

_SCHEDULE_KEYS = ("after", "before")

SEVERITIES = ("critical", "warning", "info")

# Placeholders _format() in core/rules/engine.py actually substitutes. Anything
# else survives into the stored message and the webhook payload as literal text.
MESSAGE_PLACEHOLDERS = ("class", "zone", "camera", "confidence")
_PLACEHOLDER_RE = re.compile(r"\{([^}]*)\}")


@dataclass
class RuleSchedule:
    """
    Time-of-day window a rule is active in, in UTC like every other timestamp.

    after > before means the window crosses midnight — "19:30 to 02:00" is one
    night, not an empty range. HH:MM is zero-padded, so comparing the strings
    compares the times.
    """

    after: str  # "HH:MM" UTC — window opens
    before: str  # "HH:MM" UTC — window closes, may be the next day

    def contains(self, hhmm: str) -> bool:
        if self.after <= self.before:
            return self.after <= hhmm <= self.before
        return hhmm >= self.after or hhmm <= self.before


@dataclass
class RuleConfig:
    name: str
    class_name: str  # mapped from YAML key 'class' ('class' is a Python keyword)
    cameras: list[str]  # [] = all cameras — resolved from "*" at parse time
    zones: list[str]  # [] = all zones — resolved from "*" at parse time
    min_confidence: float | None  # None = no floor beyond the model's own
    cooldown_seconds: int
    severity: str  # critical | warning | info
    notifications_table: str | None  # None = webhook fires, nothing is stored
    notify: bool  # false = filter-only rule (no webhook, no notification row)
    message: str  # supports placeholders: {class} {zone} {camera} {confidence}
    enabled: bool
    schedule: RuleSchedule | None  # None = active all day


def _parse_schedule(r: Reader) -> RuleSchedule | None:
    """
    Optional time-of-day window. Absent means the rule is active all day.

    Both ends are required together: one alone reads like a window but is not
    one. Unlike collection.yaml's schedule, after > before is allowed — that is
    how a night window is written.
    """
    if not r.has("schedule"):
        return None

    s = r.section("schedule")
    s.reject_unknown(*_SCHEDULE_KEYS)

    after = s.string_or_null("after", TIME_PATTERN, "a time as HH:MM (24-hour, UTC)")
    before = s.string_or_null("before", TIME_PATTERN, "a time as HH:MM (24-hour, UTC)")

    if not after or not before:
        return None  # string_or_null already reported the missing or malformed one
    if after == before:
        s.error(
            s.path_of("before"),
            f"before ({before}) equals after ({after}) - a window of zero length; "
            f"remove the schedule to run the rule all day",
        )
        return None

    return RuleSchedule(after=after, before=before)


def _check_message(r: Reader, message: str) -> None:
    """Reject placeholders the rules engine will never substitute."""
    if not message:
        return

    unknown = sorted(
        {
            token
            for token in _PLACEHOLDER_RE.findall(message)
            if token not in MESSAGE_PLACEHOLDERS
        }
    )
    if unknown:
        r.error(
            r.path_of("message"),
            f"unknown placeholder(s) {{{'}, {'.join(unknown)}}} - "
            f"only {', '.join('{' + p + '}' for p in MESSAGE_PLACEHOLDERS)} are substituted",
        )


def _parse_one(r: Reader) -> RuleConfig:
    r.reject_unknown(*_RULE_KEYS)

    name = r.identifier(
        "name",
        ID_PATTERN,
        "letters, digits, hyphens, and underscores only (this becomes notifications.rule_name)",
    )
    # "*" means any class, mirroring cameras/zones. Without it, every class
    # needs its own rule just to reach the detections table.
    class_name = r.string("class")
    cameras = r.string_list_or_all("cameras")
    zones = r.string_list_or_all("zones")
    min_confidence = r.number_or_null("min_confidence", minimum=0.0, maximum=1.0)
    cooldown_seconds = r.integer("cooldown_seconds", minimum=0)
    notify = r.boolean("notify")
    severity = r.enum("severity", SEVERITIES)
    notifications_table = r.string_or_null(
        "notifications_table",
        TABLE_NAME_PATTERN,
        "letters, digits, and underscores only (a table name), or null",
    )
    message = r.string("message")
    enabled = r.boolean("enabled")

    _check_message(r, message)

    return RuleConfig(
        name=name,
        class_name=ALL if class_name == ALL else class_name,
        cameras=[] if cameras == ALL else [str(c) for c in cameras],
        zones=[] if zones == ALL else [str(z) for z in zones],
        min_confidence=min_confidence,
        cooldown_seconds=cooldown_seconds,
        severity=severity,
        notifications_table=notifications_table,
        notify=notify,
        message=message,
        enabled=enabled,
        schedule=_parse_schedule(r),
    )


def parse_all(raw: Any) -> list[RuleConfig]:
    """
    Parse and validate every rule in rules.yaml.

    At least one rule must be enabled. With no enabled rules the engine's
    filter inverts - every detection of every class flows into the detections
    table instead of none - so an all-disabled file is rejected here rather
    than silently flooding storage.

    Cross-file checks (does this class/camera/zone exist anywhere) stay in
    _validate, which is the only place cameras and models are in scope.
    """
    root = Reader(_FILE, {})

    if not isinstance(raw, list):
        root.error("rules", f"expected a list of rules, got {describe(raw)}")
        root.raise_if_errors()
    if not raw:
        root.error("rules", "at least one rule must be defined")
        root.raise_if_errors()

    rules: list[RuleConfig] = []
    seen: set[str] = set()

    for i, item in enumerate(raw):
        path = f"rules[{i}]"
        rule = _parse_one(root.child(path, item))

        if rule.name:
            if rule.name in seen:
                root.error(
                    f"{path}.name",
                    f"duplicate rule name '{rule.name}' - names must be unique, "
                    f"they identify the rule in notifications.rule_name",
                )
            seen.add(rule.name)

        rules.append(rule)

    if rules and not any(rule.enabled for rule in rules):
        root.error(
            "rules",
            "every rule is disabled - with no enabled rules the filter inverts "
            "and all detections are stored; enable at least one",
        )

    root.raise_if_errors()
    return rules
