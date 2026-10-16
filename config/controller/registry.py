"""Explicit registration keeps configuration extensions easy to inspect."""

from dataclasses import dataclass
from typing import Any, Callable

from .parsers import (
    api,
    camera,
    collection,
    device,
    model,
    notifications,
    rule,
    tracker,
)


@dataclass(frozen=True)
class Section:
    name: str
    filename: str
    parse: Callable[[Any], Any]
    root_key: str | None = None
    whole_document: bool = False
    allow_empty: bool = False


# Add a parser and one registration when introducing a configuration section.
# A service does not necessarily need its own file (tracking already has one).
SECTIONS = (
    Section("device", "device.yaml", device.parse),
    Section("api", "api.yaml", api.parse),
    Section("models", "models.yaml", model.parse_all),
    Section("cameras", "cameras.yaml", camera.parse_all),
    Section("rules", "rules.yaml", rule.parse_all),
    Section("notifications", "notifications.yaml", notifications.parse),
    Section("collection", "collection.yaml", collection.parse, allow_empty=True),
    Section("tracker", "boxmot_tracker.yaml", tracker.parse, whole_document=True),
)
