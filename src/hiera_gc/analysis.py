from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from hiera_gc.config import Environment, RunConfig


@dataclass
class Warn:
    kind: str  # parse_error | dynamic_lookup | unknown_backend | ...
    message: str
    file: str = ""
    line: int = 0

    def location(self) -> str:
        if not self.file:
            return ""
        return "%s:%d" % (self.file, self.line) if self.line else self.file


@dataclass
class AnalysisResult:
    environments: List[str] = field(default_factory=list)
    keys: List["object"] = field(default_factory=list)
    orphans: List["object"] = field(default_factory=list)
    stale_files: List["object"] = field(default_factory=list)
    redundant: List["object"] = field(default_factory=list)
    shadowed: List["object"] = field(default_factory=list)
    warnings: List[Warn] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)

    @property
    def parse_errors(self) -> List[Warn]:
        return [w for w in self.warnings if w.kind == "parse_error"]

    def counts(self) -> Dict[str, int]:
        unused = [k for k in self.keys if getattr(k, "status", "") == "UNUSED"]
        return {
            "keys": len(self.keys),
            "unused": len(unused),
            "possibly_used": len(
                [k for k in self.keys
                 if getattr(k, "status", "") == "POSSIBLY_USED"]),
            "stale_params": len(
                [k for k in unused if getattr(k, "stale_param", None)]),
            "orphans": len(self.orphans),
            "stale_files": len(self.stale_files),
            "redundant": len(self.redundant),
            "shadowed": len(self.shadowed),
            "warnings": len(self.warnings),
        }

    def fails(self, fail_on: List[str]) -> bool:
        counts = self.counts()
        triggers = {
            "unused": counts["unused"],
            "possibly_used": counts["possibly_used"],
            "stale_params": counts["stale_params"],
            "orphans": counts["orphans"],
            "stale_files": counts["stale_files"],
            "redundant": counts["redundant"],
            "shadowed": counts["shadowed"],
        }
        return any(triggers.get(section, 0) for section in fail_on)

    def stats_line(self) -> str:
        parts = ["%s=%d" % (k, v) for k, v in sorted(self.stats.items())]
        return "hiera-gc stats: " + " ".join(parts)


def analyse(config: RunConfig, environments: List[Environment]) -> AnalysisResult:
    result = AnalysisResult(environments=[e.name for e in environments])
    result.stats["environments"] = len(environments)
    return result
