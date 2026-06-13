"""Text and JSON report rendering.

Everything rendered here is key names, file paths, line numbers and
reason descriptions. Data values never reach this module.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from hiera_gc.analysis import AnalysisResult

SCHEMA_VERSION = 1


def _rel(path, code_dir) -> str:
    try:
        return str(Path(path).relative_to(code_dir))
    except (ValueError, TypeError):
        return str(path)


def _location(file, line, code_dir) -> str:
    rel = _rel(file, code_dir)
    return "%s:%d" % (rel, line) if line else rel


def _group_by_name(findings) -> Dict[str, list]:
    grouped: Dict[str, list] = {}
    for finding in findings:
        grouped.setdefault(finding.key.name, []).append(finding)
    return grouped


def render_text(result: AnalysisResult, show: List[str],
                fixes=None) -> str:
    code_dir = getattr(result.config, "code_dir", None)
    out: List[str] = []
    out.append("hiera-gc report  (code: %s, %d environments: %s)" % (
        code_dir, len(result.environments),
        ", ".join(result.environments)))
    out.append("")

    active = [f for f in result.keys if not f.allowlisted]
    if "unused" in show:
        _render_unused(out, [f for f in active if f.status == "UNUSED"],
                       code_dir)
    elif "stale_params" in show:
        _render_unused(out,
                       [f for f in active
                        if f.status == "UNUSED" and f.stale_param],
                       code_dir, title="STALE PARAMETERS")
    if "possibly_used" in show:
        _render_possibly(out, active, code_dir)
    if "redundant" in show and result.redundant:
        out.append("REDUNDANT OVERRIDES (%d)" % len(result.redundant))
        for finding in result.redundant:
            out.append("  %s" % finding.key)
            out.append("    remove:       %s" % _location(
                finding.file, finding.line, code_dir))
            out.append("    identical to: %s   (envs: %s)" % (_location(
                finding.anchor_file, finding.anchor_line, code_dir),
                ", ".join(finding.envs)))
        out.append("")
    if "shadowed" in show and result.shadowed:
        out.append("SHADOWED DEFINITIONS (%d)  [likely bugs]"
                   % len(result.shadowed))
        for finding in result.shadowed:
            out.append("  %s" % finding.key)
            out.append("    never wins:  %s" % _location(
                finding.file, finding.line, code_dir))
            out.append("    shadowed by: %s   (envs: %s)" % (_location(
                finding.shadow_file, finding.shadow_line, code_dir),
                ", ".join(finding.envs)))
        out.append("")
    if "orphans" in show and result.orphans:
        out.append("ORPHANED DATA FILES (%d)  [unreachable via the "
                   "hierarchy]" % len(result.orphans))
        for orphan in result.orphans:
            out.append("  %s   (%s)" % (_rel(orphan.file, code_dir),
                                        orphan.message))
        out.append("")
    if "stale_files" in show and result.stale_files:
        out.append("STALE DATA FILES (%d)" % len(result.stale_files))
        for stale in result.stale_files:
            out.append("  %s" % _rel(stale.file, code_dir))
            out.append("    %s" % stale.message)
        out.append("")
    if "warnings" in show and result.warnings:
        out.append("WARNINGS (%d)" % len(result.warnings))
        for warn in result.warnings:
            location = _location(warn.file, warn.line, code_dir) \
                if warn.file else ""
            out.append("  [%s] %s%s" % (
                warn.kind, warn.message,
                "   (%s)" % location if location else ""))
        out.append("")

    if fixes is not None:
        _render_fixes(out, fixes, code_dir)

    counts = result.counts()
    out.append(
        "Summary: %(keys)d key definitions, %(unused)d unused "
        "(%(stale_params)d stale params), %(possibly_used)d possibly "
        "used, %(allowlisted)d allowlisted, %(redundant)d redundant, "
        "%(shadowed)d shadowed, %(orphans)d orphaned files, "
        "%(stale_files)d stale data files, %(warnings)d warnings"
        % counts)
    return "\n".join(out) + "\n"


def _render_unused(out: List[str], unused, code_dir,
                   title: str = "UNUSED KEYS") -> None:
    if not unused:
        return
    grouped = _group_by_name(unused)
    out.append("%s (%d keys, %d definitions)"
               % (title, len(grouped), len(unused)))
    for name in sorted(grouped):
        tags = []
        for finding in grouped[name]:
            if finding.stale_param:
                tags.append("STALE PARAM: %s" % finding.stale_param)
                break
        if not tags:
            for finding in grouped[name]:
                if finding.define_shape:
                    tags.append(finding.define_shape)
                    break
        out.append("  %s%s" % (name,
                               "   [%s]" % tags[0] if tags else ""))
        for finding in grouped[name]:
            out.append("    defined: %s   [%s]  unused in: %s" % (
                _location(finding.key.file, finding.key.line, code_dir),
                finding.key.layer,
                ", ".join(finding.envs) or "-"))
    out.append("")


def _render_possibly(out: List[str], findings, code_dir) -> None:
    possible = [f for f in findings if f.status == "POSSIBLY_USED"]
    if not possible:
        return
    grouped = _group_by_name(possible)
    out.append("POSSIBLY USED (%d keys, %d definitions)"
               % (len(grouped), len(possible)))
    for name in sorted(grouped):
        out.append("  %s" % name)
        for finding in grouped[name]:
            out.append("    defined: %s   [%s]" % (
                _location(finding.key.file, finding.key.line, code_dir),
                finding.key.layer))
            if finding.reason is not None:
                out.append("    why kept: %s %s   (%s)" % (
                    finding.reason.kind, finding.reason.detail,
                    _location(finding.reason.file, finding.reason.line,
                              code_dir)))
    out.append("")


def _render_fixes(out: List[str], plan, code_dir) -> None:
    removed = [a for a in plan.actions if a.action == "remove_key"]
    deleted = [a for a in plan.actions if a.action == "delete_file"]
    verb_key = "would remove" if plan.dry_run else "removed"
    verb_file = "would delete" if plan.dry_run else "deleted"
    out.append("FIXES  (environment: %s%s)"
               % (plan.env, ", dry run" if plan.dry_run else ""))
    if not removed and not deleted:
        out.append("  nothing fixable in this environment's own data "
                   "files")
    for action in removed:
        span = "%d" % action.start_line
        if action.end_line > action.start_line:
            span = "%d-%d" % (action.start_line, action.end_line)
        out.append("  %s key '%s'   %s:%s   [%s]" % (
            verb_key, action.key, _rel(action.file, code_dir), span,
            action.finding))
    for action in deleted:
        out.append("  %s file %s   [%s]" % (
            verb_file, _rel(action.file, code_dir), action.finding))
    for skip in plan.skipped:
        out.append("  skipped %s'%s' (%s): %s" % (
            "" if skip.key is None else "key ",
            skip.key if skip.key is not None
            else _rel(skip.file, code_dir),
            _rel(skip.file, code_dir) if skip.key is not None
            else skip.finding,
            skip.reason))
    if plan.out_of_scope:
        out.append("  out of scope (shared/global/module data or other "
                   "environments): %s" % ", ".join(
                       "%s=%d" % (kind, count) for kind, count
                       in sorted(plan.out_of_scope.items())))
    out.append("")


def render_json(result: AnalysisResult, show: List[str],
                fixes=None) -> str:
    code_dir = getattr(result.config, "code_dir", None)
    doc = {
        "schema_version": SCHEMA_VERSION,
        "code_dir": str(code_dir),
        "environments": result.environments,
        "summary": result.counts(),
    }

    statuses = set()
    if "unused" in show or "stale_params" in show:
        statuses.add("UNUSED")
    if "possibly_used" in show:
        statuses.add("POSSIBLY_USED")
    doc["keys"] = [
        _finding_json(finding) for finding in result.keys
        if finding.status in statuses]

    if "orphans" in show:
        doc["orphaned_files"] = [
            {"file": str(o.file), "datadir": str(o.datadir),
             "layer": o.layer, "env": o.env, "message": o.message}
            for o in result.orphans]
    if "stale_files" in show:
        doc["stale_files"] = [
            {"file": str(s.file), "env": s.env, "message": s.message}
            for s in result.stale_files]
    if "redundant" in show:
        doc["redundant"] = [
            {"key": r.key, "file": str(r.file), "line": r.line,
             "identical_to": str(r.anchor_file),
             "identical_to_line": r.anchor_line, "envs": r.envs}
            for r in result.redundant]
    if "shadowed" in show:
        doc["shadowed"] = [
            {"key": s.key, "file": str(s.file), "line": s.line,
             "shadowed_by": str(s.shadow_file),
             "shadowed_by_line": s.shadow_line, "envs": s.envs}
            for s in result.shadowed]
    if "warnings" in show:
        doc["warnings"] = [
            {"kind": w.kind, "message": w.message, "file": w.file,
             "line": w.line}
            for w in result.warnings]
    if fixes is not None:
        doc["fixes"] = {
            "environment": fixes.env,
            "dry_run": fixes.dry_run,
            "kinds": fixes.kinds,
            "actions": [
                {"action": a.action, "finding": a.finding,
                 "file": str(a.file), "key": a.key,
                 "start_line": a.start_line, "end_line": a.end_line,
                 "applied": a.applied}
                for a in fixes.actions],
            "skipped": [
                {"finding": s.finding, "file": str(s.file),
                 "key": s.key, "reason": s.reason}
                for s in fixes.skipped],
            "out_of_scope": fixes.out_of_scope,
            "errors": fixes.errors,
        }
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def _finding_json(finding) -> dict:
    reason = None
    if finding.reason is not None:
        reason = {"kind": finding.reason.kind,
                  "file": finding.reason.file,
                  "line": finding.reason.line,
                  "detail": finding.reason.detail}
    return {
        "name": finding.key.name,
        "status": finding.status,
        "file": str(finding.key.file),
        "line": finding.key.line,
        "layer": finding.key.layer,
        "env": finding.key.env,
        "module": finding.key.module,
        "envs_checked": finding.envs,
        "reason": reason,
        "stale_param": finding.stale_param,
        "define_shape": finding.define_shape,
        "allowlisted": finding.allowlisted,
    }
