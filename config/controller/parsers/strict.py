from __future__ import annotations
import logging
import math
import re
import uuid
from typing import Any

log = logging.getLogger(__name__)
_MISSING = object()
ALL = "*"
ID_PATTERN = re.compile("[A-Za-z0-9_-]+")
ZONE_NAME_PATTERN = re.compile("[A-Za-z0-9_]+")
TABLE_NAME_PATTERN = re.compile("[A-Za-z0-9_]+")
TIME_PATTERN = re.compile("([01]\\d|2[0-3]):[0-5]\\d")
DATE_PATTERN = re.compile("\\d{4}-\\d{2}-\\d{2}")
_CAMERA_URL_SCHEMES = ("rtsp://", "rtsps://", "http://", "https://")
_URL_RE = re.compile("https?://\\S+")


class ConfigError(Exception):
    """Raised when a config file fails validation."""

    def __init__(self, filename: str, problems: list[str]) -> None:
        self.filename = filename
        self.problems = problems
        count = len(problems)
        body = "\n".join((f"  {p}" for p in problems))
        super().__init__(
            f"Config error in {filename} ({count} problem{('s' if count != 1 else '')}):\n{body}"
        )


def describe(value: Any) -> str:
    """Render a rejected value for an error message, without dumping huge blobs."""
    if value is None:
        return "null"
    if isinstance(value, str):
        if not value.strip():
            return "an empty value"
        shown = value if len(value) <= 40 else value[:37] + "..."
        return f'"{shown}" (str)'
    if isinstance(value, bool):
        return f"{str(value).lower()} (bool)"
    if isinstance(value, (dict, list)):
        return f"a {type(value).__name__}"
    return f"{value!r} ({type(value).__name__})"


class Reader:
    """Strict field reader for one config file."""

    def __init__(
        self,
        filename: str,
        raw: Any,
        path: str = "",
        _errors: list[tuple[str, str]] | None = None,
        _warnings: list[tuple[str, str]] | None = None,
    ) -> None:
        self._filename = filename
        self._path = path
        self._errors = _errors if _errors is not None else []
        self._warnings = _warnings if _warnings is not None else []
        if isinstance(raw, dict):
            self._raw: dict = raw
        else:
            self._raw = {}
            self._err(path or "(root)", f"expected a mapping, got {describe(raw)}")

    @property
    def error_count(self) -> int:
        """Errors recorded so far, shared across this file's readers."""
        return len(self._errors)

    @property
    def raw(self) -> dict:
        """The underlying mapping, for presence checks on mutually exclusive keys."""
        return self._raw

    def _key_path(self, key: str) -> str:
        return f"{self._path}.{key}" if self._path else key

    def _err(self, path: str, message: str) -> None:
        self._errors.append((path, message))

    def _get(self, key: str) -> Any:
        path = self._key_path(key)
        if key not in self._raw:
            self._err(path, "required key is missing")
            return _MISSING
        return self._raw[key]

    def has(self, key: str) -> bool:
        """Whether the key is present at all — for optional sections."""
        return self._raw.get(key, _MISSING) is not _MISSING

    def section(self, key: str) -> "Reader":
        """A required nested mapping. Shares this reader's error list."""
        path = self._key_path(key)
        value = self._raw.get(key, _MISSING)
        if value is _MISSING:
            self._err(path, "required section is missing")
            value = {}
        elif not isinstance(value, dict):
            self._err(path, f"expected a mapping, got {describe(value)}")
            value = {}
        return Reader(self._filename, value, path, self._errors, self._warnings)

    def string(self, key: str, *, min_len: int = 1) -> str:
        """A required non-empty string. Surrounding whitespace is stripped."""
        path = self._key_path(key)
        value = self._get(key)
        if value is _MISSING:
            return ""
        if not isinstance(value, str):
            self._err(path, f"expected a string, got {describe(value)}")
            return ""
        stripped = value.strip()
        if not stripped:
            self._err(path, "expected a non-empty string, got an empty value")
            return ""
        if len(stripped) < min_len:
            self._err(
                path,
                f"expected a string of at least {min_len} characters, got {len(stripped)}",
            )
            return ""
        return stripped

    def integer(
        self, key: str, *, minimum: int | None = None, maximum: int | None = None
    ) -> int:
        """A required integer, optionally range-checked."""
        path = self._key_path(key)
        value = self._get(key)
        fallback = minimum if minimum is not None else 0
        if value is _MISSING:
            return fallback
        if isinstance(value, bool) or not isinstance(value, int):
            self._err(path, f"expected an integer, got {describe(value)}")
            return fallback
        if isinstance(value, float) and not math.isfinite(value):
            self._err(path, "expected a finite number")
            return fallback
        if minimum is not None and value < minimum:
            self._err(path, f"expected an integer >= {minimum}, got {value}")
            return minimum
        if maximum is not None and value > maximum:
            self._err(path, f"expected an integer <= {maximum}, got {value}")
            return maximum
        return value

    def number(
        self, key: str, *, minimum: float | None = None, maximum: float | None = None
    ) -> float:
        """A required number (int or float), optionally range-checked."""
        path = self._key_path(key)
        value = self._get(key)
        fallback = float(minimum) if minimum is not None else 0.0
        if value is _MISSING:
            return fallback
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self._err(path, f"expected a number, got {describe(value)}")
            return fallback
        if isinstance(value, float) and not math.isfinite(value):
            self._err(path, "expected a finite number")
            return fallback
        if minimum is not None and value < minimum:
            self._err(path, f"expected a number >= {minimum}, got {value}")
            return float(minimum)
        if maximum is not None and value > maximum:
            self._err(path, f"expected a number <= {maximum}, got {value}")
            return float(maximum)
        return float(value)

    def int_pair(self, key: str, *, minimum: int = 1) -> list[int]:
        """A required [a, b] pair of integers, each >= minimum."""
        path = self._key_path(key)
        value = self._get(key)
        if value is _MISSING:
            return [minimum, minimum]
        if not isinstance(value, list) or len(value) != 2:
            self._err(
                path,
                f"expected exactly two values [width, height], got {describe(value)}",
            )
            return [minimum, minimum]
        a, b = value
        if any(
            (
                isinstance(v, bool) or not isinstance(v, int) or v < minimum
                for v in (a, b)
            )
        ):
            self._err(path, f"expected two integers >= {minimum}, got [{a!r}, {b!r}]")
            return [minimum, minimum]
        return [a, b]

    def string_list(self, key: str, *, unique: bool = True) -> list[str]:
        """A required non-empty list of non-empty strings."""
        path = self._key_path(key)
        value = self._get(key)
        if value is _MISSING:
            return []
        if isinstance(value, str):
            if value.strip() == ALL:
                self._err(
                    path, f'"{ALL}" is not allowed here - list the values explicitly'
                )
            else:
                self._err(path, f"expected a list, got {describe(value)}")
            return []
        if not isinstance(value, list):
            self._err(path, f"expected a list, got {describe(value)}")
            return []
        if not value:
            self._err(path, "expected a non-empty list, got an empty list")
            return []
        out: list[str] = []
        for i, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                self._err(
                    f"{path}[{i}]", f"expected a non-empty string, got {describe(item)}"
                )
                continue
            cleaned = item.strip()
            if unique and cleaned in out:
                self._err(f"{path}[{i}]", f"duplicate entry '{cleaned}'")
                continue
            out.append(cleaned)
        return out

    def number_or_null(
        self, key: str, *, minimum: float | None = None, maximum: float | None = None
    ) -> float | None:
        """A required key whose value is a number or an explicit null."""
        path = self._key_path(key)
        value = self._get(key)
        if value is _MISSING:
            return None
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self._err(path, f"expected a number or null, got {describe(value)}")
            return None
        if not math.isfinite(value):
            self._err(path, "expected a finite number or null")
            return None
        if minimum is not None and value < minimum:
            self._err(path, f"expected a number >= {minimum} or null, got {value}")
            return None
        if maximum is not None and value > maximum:
            self._err(path, f"expected a number <= {maximum} or null, got {value}")
            return None
        return float(value)

    def integer_or_null(
        self, key: str, *, minimum: int | None = None, maximum: int | None = None
    ) -> int | None:
        """A required key whose value is an integer or an explicit null."""
        path = self._key_path(key)
        value = self._get(key)
        if value is _MISSING or value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            self._err(path, f"expected an integer or null, got {describe(value)}")
            return None
        if minimum is not None and value < minimum:
            self._err(path, f"expected an integer >= {minimum} or null, got {value}")
            return None
        if maximum is not None and value > maximum:
            self._err(path, f"expected an integer <= {maximum} or null, got {value}")
            return None
        return value

    def string_or_null(
        self, key: str, pattern: re.Pattern | None = None, allowed: str = ""
    ) -> str | None:
        """A required key whose value is a non-empty string or an explicit null."""
        path = self._key_path(key)
        value = self._get(key)
        if value is _MISSING or value is None:
            return None
        if not isinstance(value, str):
            self._err(path, f"expected a string or null, got {describe(value)}")
            return None
        stripped = value.strip()
        if not stripped:
            self._err(path, "expected a non-empty string or null, got an empty value")
            return None
        if pattern is not None and (not pattern.fullmatch(stripped)):
            self._err(path, f"expected {allowed}, got {describe(value)}")
            return None
        return stripped

    def optional_string(self, key: str) -> str | None:
        """A key that may be absent entirely, or present as a non-empty string."""
        path = self._key_path(key)
        if key not in self._raw:
            return None
        value = self._raw[key]
        if value is None:
            return None
        if not isinstance(value, str):
            self._err(path, f"expected a string, got {describe(value)}")
            return None
        stripped = value.strip()
        if not stripped:
            self._err(path, "expected a non-empty string, or omit the key entirely")
            return None
        return stripped

    def uuid_string(self, key: str) -> str:
        """A required UUID string. Catches truncated or mangled pastes."""
        path = self._key_path(key)
        value = self._get(key)
        if value is _MISSING:
            return ""
        if not isinstance(value, str) or not value.strip():
            self._err(path, f"expected a UUID string, got {describe(value)}")
            return ""
        stripped = value.strip()
        try:
            uuid.UUID(stripped)
        except (ValueError, TypeError):
            self._err(path, f"expected a UUID string, got {describe(value)}")
            return ""
        return stripped

    def url(self, key: str) -> str:
        """A required http(s) URL. Any trailing slash is stripped after validation."""
        path = self._key_path(key)
        value = self._get(key)
        if value is _MISSING:
            return ""
        if not isinstance(value, str):
            self._err(path, f"expected a URL string, got {describe(value)}")
            return ""
        stripped = value.strip()
        if not _URL_RE.fullmatch(stripped):
            self._err(
                path,
                f"expected a URL starting with http:// or https://, got {describe(value)}",
            )
            return ""
        return stripped.rstrip("/")

    def boolean(self, key: str) -> bool:
        """A required real boolean. Strings like "true" and ints like 1 are rejected."""
        path = self._key_path(key)
        value = self._get(key)
        if value is _MISSING:
            return False
        if not isinstance(value, bool):
            self._err(path, f"expected true or false, got {describe(value)}")
            return False
        return value

    def enum(self, key: str, allowed: tuple[str, ...]) -> str:
        """A required string from a fixed set. Case-sensitive."""
        path = self._key_path(key)
        value = self.string(key)
        if not value:
            return allowed[0]
        if value not in allowed:
            self._err(
                path, f"expected one of {' | '.join(allowed)}, got {describe(value)}"
            )
            return allowed[0]
        return value

    def identifier(self, key: str, pattern: re.Pattern, allowed: str) -> str:
        """A required non-empty string restricted to a safe character set."""
        path = self._key_path(key)
        value = self.string(key)
        if not value:
            return ""
        if not pattern.fullmatch(value):
            self._err(path, f"expected {allowed}, got {describe(value)}")
            return ""
        return value

    def camera_source(self, key: str) -> str | int:
        path = self._key_path(key)
        value = self._get(key)
        if value is _MISSING:
            return 0
        if type(value) is int and value >= 0:
            return value
        if isinstance(value, str) and value.strip():
            value = value.strip()
            if value.isdigit():
                self._err(
                    path, "USB device indices must be integers, not quoted strings"
                )
                return 0
            if any((character.isspace() for character in value)) and value.startswith(
                _CAMERA_URL_SCHEMES
            ):
                self._err(path, "stream URLs must not contain whitespace")
                return 0
            if "://" in value and (not value.startswith(_CAMERA_URL_SCHEMES)):
                self._err(path, "unsupported stream URL scheme")
                return 0
            return value
        self._err(
            path, "expected a non-negative USB index, stream URL, or video file path"
        )
        return 0

    def list_or_all(self, key: str) -> str | list:
        """Either the literal "*" or a non-empty list. Returns ALL or the raw list."""
        path = self._key_path(key)
        value = self._get(key)
        if value is _MISSING:
            return []
        if isinstance(value, str):
            if value.strip() == ALL:
                return ALL
            self._err(path, f'expected "{ALL}" or a list, got {describe(value)}')
            return []
        if not isinstance(value, list):
            self._err(path, f'expected "{ALL}" or a list, got {describe(value)}')
            return []
        if not value:
            self._err(path, f'expected "{ALL}" or a non-empty list, got an empty list')
            return []
        return value

    def string_list_or_all(self, key: str) -> str | list[str]:
        """list_or_all, with every entry required to be a non-empty string."""
        path = self._key_path(key)
        value = self.list_or_all(key)
        if value == ALL or not value:
            return value
        out: list[str] = []
        for i, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                self._err(
                    f"{path}[{i}]", f"expected a non-empty string, got {describe(item)}"
                )
                continue
            out.append(item.strip())
        return out

    def polygon(self, key: str, *, min_points: int = 3) -> list[list[int]]:
        """A required polygon: at least min_points entries of [x, y], both >= 0."""
        path = self._key_path(key)
        value = self._get(key)
        if value is _MISSING:
            return []
        if not isinstance(value, list):
            self._err(path, f"expected a list of [x, y] points, got {describe(value)}")
            return []
        if len(value) < min_points:
            self._err(
                path,
                f"expected at least {min_points} points to form a polygon, got {len(value)}",
            )
            return []
        out: list[list[int]] = []
        for i, point in enumerate(value):
            where = f"{path}[{i}]"
            if not isinstance(point, list) or len(point) != 2:
                self._err(
                    where, f"expected exactly two values [x, y], got {describe(point)}"
                )
                continue
            x, y = point
            bad = [
                name
                for name, v in (("x", x), ("y", y))
                if isinstance(v, bool) or not isinstance(v, int) or v < 0
            ]
            if bad:
                self._err(
                    where,
                    f"expected non-negative integers for x and y, got [{x!r}, {y!r}]",
                )
                continue
            out.append([x, y])
        return out

    def child(self, path: str, raw: Any) -> "Reader":
        """A reader for a nested item, sharing this reader's errors and warnings."""
        return Reader(self._filename, raw, path, self._errors, self._warnings)

    def reject_unknown(self, *known: str) -> None:
        for key in sorted(self._raw, key=str):
            if key not in known:
                self._err(self._key_path(str(key)), "unknown key")

    def error(self, path: str, message: str) -> None:
        """Record a problem found outside a single field (e.g. a duplicate id)."""
        self._err(path, message)

    def path_of(self, key: str) -> str:
        """The fully qualified path of a key, for use with error()."""
        return self._key_path(key)

    def raw_value(self, key: str):
        """The value as written, or None if the key is absent. Records nothing."""
        return self._raw.get(key)

    def warn(self, key: str, message: str) -> None:
        """Record a non-fatal concern. Logged at raise_if_errors() time."""
        self._warnings.append((self._key_path(key), message))

    def raise_if_errors(self) -> None:
        """Log any warnings, then raise once with every error found in the file."""
        for path, message in self._warnings:
            log.warning("%s: %s - %s", self._filename, path, message)
        if not self._errors:
            return
        width = max((len(path) for path, _ in self._errors))
        problems = [
            f"{path.ljust(width)} - {message}" for path, message in self._errors
        ]
        raise ConfigError(self._filename, problems)
