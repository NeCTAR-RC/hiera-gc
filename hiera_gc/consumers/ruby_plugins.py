"""Regex extraction of lookups from Ruby plugins
(lib/puppet/**/*.rb in modules: functions, types, providers)."""

from __future__ import annotations

from pathlib import Path
import re

from hiera_gc.consumers.model import Consumer

CALL_FUNCTION = re.compile(
    r"""call_function\(\s*['"]lookup['"]\s*,\s*\[?\s*['"]([^'"]+)['"]"""
)
LOOKUPVAR = re.compile(r"""lookupvar\(\s*['"](?:::)?([\w:]+)['"]""")


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def extract_ruby(text: str, file: Path) -> list[Consumer]:
    consumers = []
    for match in CALL_FUNCTION.finditer(text):
        consumers.append(
            Consumer(
                kind="ruby_lookup",
                key=match.group(1),
                pattern=None,
                file=file,
                line=_line(text, match.start()),
                detail="call_function lookup",
            )
        )
    for match in LOOKUPVAR.finditer(text):
        consumers.append(
            Consumer(
                kind="ruby_var",
                key=match.group(1),
                pattern=None,
                file=file,
                line=_line(text, match.start()),
                detail="lookupvar",
            )
        )
    return consumers
