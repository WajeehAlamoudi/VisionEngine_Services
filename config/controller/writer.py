"""Atomic YAML artifact writing, separate from parsing and graph compilation."""

from collections.abc import Mapping
import os
from pathlib import Path
import tempfile
from typing import Any, Callable

import yaml


def space_yaml_blocks(content: str) -> str:
    """Separate sections, service/profile definitions, and mapping list items."""
    breaks = set()

    def visit(node, section=False):
        if isinstance(node, yaml.MappingNode):
            for index, (key, value) in enumerate(node.value):
                if section and index:
                    breaks.add(key.start_mark.line)
                visit(value, key.value in ("graph", "services", "profiles"))
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                if isinstance(item, yaml.MappingNode):
                    breaks.add(item.start_mark.line)
                visit(item)

    visit(yaml.compose(content), section=True)
    source = content.splitlines()
    # Keep comments attached to the block they describe.
    for index in list(breaks):
        while index > 0 and source[index - 1].lstrip().startswith("#"):
            breaks.discard(index)
            index -= 1
            breaks.add(index)
    lines = []
    for index, line in enumerate(source):
        if index in breaks and lines and lines[-1].strip():
            lines.append("")
        lines.append(line)
    return "\n".join(lines) + "\n"


def dump_yaml(document: Mapping[str, Any]) -> str:
    """Format YAML with blank lines between logical blocks."""
    content = yaml.safe_dump(dict(document), sort_keys=False, allow_unicode=True)
    return space_yaml_blocks(content)


def write_yaml(
    path: str | Path,
    document: Mapping[str, Any],
    *,
    formatter: Callable[[Mapping[str, Any]], str] = dump_yaml,
) -> bool:
    """Write a prepared artifact; return False when its contents are unchanged.

    Serialization finishes before touching the destination. Replace atomically
    so a failed write cannot truncate a previously valid artifact. Callers must
    validate/compile their data before calling this function. This writer does
    not invent a pipeline schema or validate runtime graph semantics.
    """
    if not isinstance(document, Mapping):
        raise TypeError("generated artifacts must be mappings")
    content = formatter(document).encode("utf-8")
    target = Path(path)
    try:
        if target.read_bytes() == content:
            return False
    except FileNotFoundError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True
