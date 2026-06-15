"""Realpath-keyed extraction cache.

Global modules are visible to every environment; with many
environments each file must still be read and parsed exactly once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

from hiera_gc.analysis import Warn
from hiera_gc.consumers.epp import extract_epp
from hiera_gc.consumers.erb import extract_erb
from hiera_gc.consumers.model import Consumer
from hiera_gc.consumers.pp_classes import PPDefinitions, extract_definitions
from hiera_gc.consumers.pp_lookups import extract_lookups
from hiera_gc.consumers.pp_tokens import tokenize
from hiera_gc.consumers.ruby_plugins import extract_ruby

#: Anything that could be a hiera key name appearing verbatim in text.
MENTION_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_:.\-]*")
#: Locations kept per token; enough that a key's own definitions cannot
#: crowd out a real mention elsewhere.
MENTION_CAP = 8

MentionMap = Dict[str, List[Tuple[Path, int]]]


@dataclass
class PPFileResult:
    defs: PPDefinitions
    consumers: list[Consumer]
    mentions: MentionMap


@dataclass
class FileResult:
    consumers: list[Consumer]
    mentions: MentionMap


def mention_map(text: str, file: Path) -> MentionMap:
    mentions: MentionMap = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in MENTION_TOKEN.finditer(line):
            token = match.group(0).rstrip(".-")
            slots = mentions.setdefault(token, [])
            if len(slots) < MENTION_CAP:
                slots.append((file, lineno))
    return mentions


@dataclass
class ExtractorCache:
    warnings: list[Warn] = field(default_factory=list)
    _pp: dict[Path, PPFileResult | None] = field(default_factory=dict)
    _other: dict[Path, FileResult | None] = field(default_factory=dict)
    _data_mentions: dict[Path, MentionMap] = field(default_factory=dict)
    _data_consumers: dict[Path, tuple] = field(default_factory=dict)

    def _read(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self.warnings.append(
                Warn(
                    "parse_error", f"cannot read {path}: {exc}", file=str(path)
                )
            )
            return None

    def pp(self, path: Path) -> PPFileResult | None:
        real = path.resolve()
        if real not in self._pp:
            text = self._read(path)
            if text is None:
                self._pp[real] = None
            else:
                tokens = tokenize(text)
                self._pp[real] = PPFileResult(
                    defs=extract_definitions(tokens),
                    consumers=extract_lookups(tokens, path),
                    mentions=mention_map(text, path),
                )
        return self._pp[real]

    def template_or_ruby(self, path: Path) -> FileResult | None:
        real = path.resolve()
        if real not in self._other:
            text = self._read(path)
            if text is None:
                self._other[real] = None
            else:
                if path.suffix == ".epp":
                    consumers = extract_epp(text, path)
                elif path.suffix == ".erb":
                    consumers = extract_erb(text, path)
                else:
                    consumers = extract_ruby(text, path)
                self._other[real] = FileResult(
                    consumers=consumers, mentions=mention_map(text, path)
                )
        return self._other[real]

    def data_mentions(self, path: Path) -> MentionMap:
        real = path.resolve()
        if real not in self._data_mentions:
            text = self._read(path)
            self._data_mentions[real] = (
                mention_map(text, path) if text is not None else {}
            )
        return self._data_mentions[real]

    def data_consumers(self, path: Path, doc):
        real = path.resolve()
        if real not in self._data_consumers:
            from hiera_gc.consumers.data_interp import extract_data_consumers

            self._data_consumers[real] = extract_data_consumers(doc)
        return self._data_consumers[real]
