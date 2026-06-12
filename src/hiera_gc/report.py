from __future__ import annotations

import json
from typing import List

from hiera_gc.analysis import AnalysisResult

SCHEMA_VERSION = 1


def render_json(result: AnalysisResult, show: List[str]) -> str:
    doc = {
        "schema_version": SCHEMA_VERSION,
        "environments": result.environments,
        "summary": result.counts(),
    }
    if "warnings" in show:
        doc["warnings"] = [
            {"kind": w.kind, "message": w.message,
             "file": w.file, "line": w.line}
            for w in result.warnings
        ]
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def render_text(result: AnalysisResult, show: List[str]) -> str:
    lines = []
    lines.append("hiera-gc report  (%d environments: %s)"
                 % (len(result.environments), ", ".join(result.environments)))
    lines.append("")

    if "warnings" in show and result.warnings:
        lines.append("WARNINGS (%d)" % len(result.warnings))
        for warn in result.warnings:
            loc = warn.location()
            lines.append("  %s%s" % (warn.message,
                                     "  [%s]" % loc if loc else ""))
        lines.append("")

    counts = result.counts()
    lines.append(
        "Summary: %(keys)d keys, %(unused)d unused, "
        "%(possibly_used)d possibly used, %(stale_params)d stale params, "
        "%(orphans)d orphaned files, %(stale_files)d stale data files, "
        "%(redundant)d redundant, %(shadowed)d shadowed, "
        "%(warnings)d warnings" % counts)
    return "\n".join(lines) + "\n"
