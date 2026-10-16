from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .strict import (
    ALL,
    DATE_PATTERN,
    ID_PATTERN,
    TIME_PATTERN,
    Reader,
    describe,
)

_FILE = "collection.yaml"

_TOP_KEYS = ("output_dir", "sessions")
_SESSION_KEYS = (
    "id",
    "camera",
    "enabled",
    "schedule",
    "sampling",
    "filters",
    "save",
    "max_frames",
)
_SCHEDULE_KEYS = ("after", "before", "start_date", "end_date")
_SAMPLING_KEYS = ("mode", "interval_seconds", "frames_per_minute")
_FILTERS_KEYS = ("classes", "min_confidence", "min_detections")
_SAVE_KEYS = ("annotated", "raw", "metadata")

SAMPLING_MODES = ("interval", "on_detection", "random")

_DEFAULT_OUTPUT_DIR = "./collected"


@dataclass
class ScheduleConfig:
    after: str | None  # HH:MM â€” time of day window start
    before: str | None  # HH:MM â€” time of day window end
    start_date: str | None  # YYYY-MM-DD â€” calendar range start
    end_date: str | None  # YYYY-MM-DD â€” calendar range end


@dataclass
class SamplingConfig:
    mode: str  # interval | on_detection | random
    interval_seconds: int | None
    frames_per_minute: int | None


@dataclass
class FiltersConfig:
    classes: list[str]  # [] = all classes â€” resolved from "*"
    min_confidence: float
    min_detections: int


@dataclass
class SaveConfig:
    annotated: bool  # frame with bounding boxes drawn
    raw: bool  # clean frame, no annotations
    metadata: bool  # JSON sidecar with detection details


@dataclass
class CollectionSession:
    id: str
    camera: str
    enabled: bool
    schedule: ScheduleConfig
    sampling: SamplingConfig
    filters: FiltersConfig
    save: SaveConfig
    max_frames: int  # 0 = unlimited


@dataclass
class CollectionConfig:
    output_dir: str
    sessions: list[CollectionSession]


def _real_date(r: Reader, key: str, value: str | None) -> str | None:
    """DATE_PATTERN accepts 2026-02-30; this rejects it."""
    if value is None:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        r.error(r.path_of(key), f"'{value}' is not a real calendar date")
        return None
    return value


def _parse_schedule(r: Reader) -> ScheduleConfig:
    s = r.section("schedule")
    s.reject_unknown(*_SCHEDULE_KEYS)

    after = s.string_or_null(
        "after", TIME_PATTERN, "a time as HH:MM (24-hour), or null"
    )
    before = s.string_or_null(
        "before", TIME_PATTERN, "a time as HH:MM (24-hour), or null"
    )
    start_date = _real_date(
        s,
        "start_date",
        s.string_or_null("start_date", DATE_PATTERN, "a date as YYYY-MM-DD, or null"),
    )
    end_date = _real_date(
        s,
        "end_date",
        s.string_or_null("end_date", DATE_PATTERN, "a date as YYYY-MM-DD, or null"),
    )

    if after and before and after >= before:
        s.error(
            s.path_of("before"), f"before ({before}) must be later than after ({after})"
        )
    if start_date and end_date and start_date > end_date:
        s.error(
            s.path_of("end_date"),
            f"end_date ({end_date}) is before start_date ({start_date})",
        )

    return ScheduleConfig(
        after=after, before=before, start_date=start_date, end_date=end_date
    )


def _parse_sampling(r: Reader) -> SamplingConfig:
    s = r.section("sampling")
    s.reject_unknown(*_SAMPLING_KEYS)

    mode = s.enum("mode", SAMPLING_MODES)
    interval_seconds = s.integer_or_null("interval_seconds", minimum=1)
    frames_per_minute = s.integer_or_null("frames_per_minute", minimum=1)

    # Each mode needs its own rate. Without this, `mode: interval` with a null
    # interval parsed fine and left the collector with no interval to use.
    if mode == "interval" and interval_seconds is None:
        s.error(s.path_of("interval_seconds"), "required when mode is 'interval'")
    if mode == "random" and frames_per_minute is None:
        s.error(s.path_of("frames_per_minute"), "required when mode is 'random'")

    return SamplingConfig(
        mode=mode,
        interval_seconds=interval_seconds,
        frames_per_minute=frames_per_minute,
    )


def _parse_filters(r: Reader) -> FiltersConfig:
    f = r.section("filters")
    f.reject_unknown(*_FILTERS_KEYS)

    classes = f.string_list_or_all("classes")
    return FiltersConfig(
        classes=[] if classes == ALL else list(classes),
        min_confidence=f.number("min_confidence", minimum=0.0, maximum=1.0),
        min_detections=f.integer("min_detections", minimum=0),
    )


def _parse_save(r: Reader) -> SaveConfig:
    s = r.section("save")
    s.reject_unknown(*_SAVE_KEYS)

    save = SaveConfig(
        annotated=s.boolean("annotated"),
        raw=s.boolean("raw"),
        metadata=s.boolean("metadata"),
    )
    if save.annotated:
        s.error(
            s.path_of("annotated"),
            "preview images are not supported; set false for clean image + YOLO label collection",
        )
    if not save.raw:
        s.error(
            s.path_of("raw"),
            "must be true: each YOLO annotation requires its original image",
        )
    if not (save.annotated or save.raw or save.metadata):
        # reported against the parent, so the path reads sessions[N].save
        r.error(
            r.path_of("save"),
            "all three are false - this session would write nothing to disk",
        )
    return save


def _parse_session(r: Reader) -> CollectionSession:
    r.reject_unknown(*_SESSION_KEYS)

    return CollectionSession(
        id=r.identifier(
            "id",
            ID_PATTERN,
            "letters, digits, hyphens, and underscores only (used as a folder name)",
        ),
        camera=r.string("camera"),
        enabled=r.boolean("enabled"),
        schedule=_parse_schedule(r),
        sampling=_parse_sampling(r),
        filters=_parse_filters(r),
        save=_parse_save(r),
        max_frames=r.integer("max_frames", minimum=0),
    )


def parse(raw: Any) -> CollectionConfig:
    """
    Parse and validate collection.yaml.

    Collection is an optional parallel feature, so an empty file (raw is None)
    means "not configured" rather than an error - the only file allowed to be
    blank. An empty `sessions:` list means the same thing explicitly.

    Whether a session's camera exists is checked in _validate, where
    cameras.yaml is in scope.
    """
    if raw is None:
        return CollectionConfig(output_dir=_DEFAULT_OUTPUT_DIR, sessions=[])

    r = Reader(_FILE, raw)
    r.reject_unknown(*_TOP_KEYS)

    output_dir = r.string("output_dir")
    raw_sessions = r.raw.get("sessions", None)
    if "sessions" not in r.raw:
        r.error(
            "sessions", "required key is missing (use an empty list for no sessions)"
        )
        raw_sessions = []
    elif not isinstance(raw_sessions, list):
        r.error(
            "sessions", f"expected a list of sessions, got {describe(raw_sessions)}"
        )
        raw_sessions = []

    sessions: list[CollectionSession] = []
    seen: set[str] = set()

    for i, item in enumerate(raw_sessions):
        path = f"sessions[{i}]"
        session = _parse_session(r.child(path, item))

        if session.id:
            if session.id in seen:
                r.error(
                    f"{path}.id",
                    f"duplicate session id '{session.id}' - ids become folder names",
                )
            seen.add(session.id)

        sessions.append(session)

    r.raise_if_errors()
    return CollectionConfig(output_dir=output_dir, sessions=sessions)
