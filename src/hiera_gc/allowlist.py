from __future__ import annotations

import re
from pathlib import Path
from typing import List

from hiera_gc.analysis import Warn


def load_allowlist(path: Path, warnings: List[Warn]) -> List["re.Pattern"]:
    patterns = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        warnings.append(Warn("config", "cannot read allowlist %s: %s"
                             % (path, exc)))
        return patterns
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(line))
        except re.error as exc:
            warnings.append(Warn(
                "config", "allowlist %s line %d: bad regex: %s"
                % (path, lineno, exc), file=str(path), line=lineno))
    return patterns


def is_allowlisted(name: str, patterns: List["re.Pattern"]) -> bool:
    return any(p.fullmatch(name) for p in patterns)
