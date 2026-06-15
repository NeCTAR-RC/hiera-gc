"""Regex extraction of lookups and variable references from ERB
templates."""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

from hiera_gc.consumers.model import Consumer

_COMMENT_RE = re.compile(r"<%#.*?%>", re.DOTALL)
_NON_NEWLINE = re.compile(r"[^\n]")

CALL_FUNCTION = re.compile(
    r"""call_function\(\s*['"]lookup['"]\s*,\s*\[?\s*['"]([^'"]+)['"]""")
FUNCTION_HIERA = re.compile(
    r"""function_(hiera(?:_array|_hash|_include)?)"""
    r"""\(\s*\[\s*['"]([^'"]+)['"]""")
SCOPE_INDEX = re.compile(r"""scope\[\s*['"](?:::)?([\w:]+)['"]\s*\]""")
LOOKUPVAR = re.compile(r"""scope\.lookupvar\(\s*['"](?:::)?([\w:]+)['"]""")


def _mask_comments(text: str) -> str:
    return _COMMENT_RE.sub(lambda m: _NON_NEWLINE.sub(" ", m.group(0)),
                           text)


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def extract_erb(text: str, file: Path) -> List[Consumer]:
    text = _mask_comments(text)
    consumers = []
    for match in CALL_FUNCTION.finditer(text):
        consumers.append(Consumer(
            kind="erb_lookup", key=match.group(1), pattern=None, file=file,
            line=_line(text, match.start()), detail="call_function lookup"))
    for match in FUNCTION_HIERA.finditer(text):
        func = match.group(1)
        consumers.append(Consumer(
            kind="erb_lookup", key=match.group(2), pattern=None, file=file,
            line=_line(text, match.start()), detail="function_%s" % func,
            merge=func in ("hiera_array", "hiera_hash")))
    for match in SCOPE_INDEX.finditer(text):
        consumers.append(Consumer(
            kind="erb_var", key=match.group(1), pattern=None, file=file,
            line=_line(text, match.start()), detail="scope[...]"))
    for match in LOOKUPVAR.finditer(text):
        consumers.append(Consumer(
            kind="erb_var", key=match.group(1), pattern=None, file=file,
            line=_line(text, match.start()), detail="scope.lookupvar"))
    return consumers
