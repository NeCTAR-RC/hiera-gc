from __future__ import annotations

from pathlib import Path
import re

from hiera_gc.analysis import Warn


def load_allowlist(path: Path, warnings: list[Warn]) -> list[re.Pattern]:
    patterns = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        warnings.append(Warn("config", f"cannot read allowlist {path}: {exc}"))
        return patterns
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(line))
        except re.error as exc:
            warnings.append(
                Warn(
                    "config",
                    f"allowlist {path} line {lineno}: bad regex: {exc}",
                    file=str(path),
                    line=lineno,
                )
            )
    return patterns


def is_allowlisted(name: str, patterns: list[re.Pattern]) -> bool:
    return any(p.fullmatch(name) for p in patterns)
