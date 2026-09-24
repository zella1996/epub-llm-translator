"""Load local key/value configuration without adding third-party dependencies."""

from __future__ import annotations

from pathlib import Path


DEFAULT_PROPERTIES_FILE = Path("secrets.properties")


def load_properties(path: Path) -> dict[str, str]:
    """Read a small Java-properties-style file.

    Blank lines and lines beginning with ``#`` or ``!`` are ignored. Values may
    contain additional ``=`` or ``:`` characters.
    """

    if not path.is_file():
        return {}

    properties: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith(("#", "!")):
            continue

        equals = line.find("=")
        colon = line.find(":")
        separators = [index for index in (equals, colon) if index >= 0]
        if not separators:
            raise ValueError(f"properties 第 {line_number} 行缺少 = 或 :")

        separator = min(separators)
        key = line[:separator].strip()
        value = line[separator + 1 :].strip()
        if not key:
            raise ValueError(f"properties 第 {line_number} 行的键为空")
        properties[key] = value
    return properties
