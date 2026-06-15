"""Applying fixes for findings, one environment at a time.

A fix run is restricted to data files inside a single environment's
own directory so each run produces one reviewable change in one repo.
Shared, global and module layer data is visible to other environments
(module data is usually vendored by r10k as well) and is never
modified; such findings are tallied as out of scope instead.

Key removals are line-based edits computed from the composed YAML node
marks, so comments, ordering, anchors and eyaml ENC[...] values
elsewhere in the file survive untouched. Whole files are deleted only
for orphaned and stale data files. Nothing in this module prints or
stores data values.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import yaml

from hiera_gc.analysis import AnalysisResult
from hiera_gc.yamlloc import LoadedDoc, TopKey

#: Finding kinds --fix may act on; shadowed definitions are excluded
#: deliberately (they are likely bugs and the right fix is ambiguous).
FIX_KINDS = ["unused", "stale_params", "redundant", "orphans",
             "stale_files"]
DEFAULT_FIX_KINDS = "unused,redundant,orphans,stale_files"

ACTION_REMOVE_KEY = "remove_key"
ACTION_DELETE_FILE = "delete_file"


@dataclass
class FixAction:
    action: str  # remove_key | delete_file
    finding: str  # unused | stale_param | redundant | orphan | stale_file
    file: Path
    key: Optional[str] = None
    start_line: int = 0  # 1-based, inclusive
    end_line: int = 0  # 1-based, inclusive
    applied: bool = False


@dataclass
class FixSkip:
    finding: str
    file: Path
    key: Optional[str]
    reason: str


@dataclass
class FixPlan:
    env: str
    kinds: List[str]
    dry_run: bool = False
    actions: List[FixAction] = field(default_factory=list)
    skipped: List[FixSkip] = field(default_factory=list)
    #: Findings of a requested kind that live outside the target
    #: environment's own data files, counted per kind.
    out_of_scope: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)


def plan_fixes(result: AnalysisResult, env: str,
               kinds: List[str]) -> FixPlan:
    """Build the fix plan for exactly one environment.

    Only findings in data files belonging to the environment layer of
    `env` produce actions; everything else is counted out of scope.
    """
    if env not in result.environments:
        raise ValueError(
            "fix environment %r is not among the analysed environments"
            % env)
    plan = FixPlan(env=env, kinds=list(kinds))
    inventory = result.inventory

    deletions: Set[Path] = set()
    if "orphans" in kinds:
        for orphan in result.orphans:
            if orphan.layer == "environment" and orphan.env == env:
                plan.actions.append(FixAction(
                    ACTION_DELETE_FILE, "orphan", orphan.file))
                deletions.add(orphan.file)
            else:
                _out_of_scope(plan, "orphans")
    if "stale_files" in kinds:
        for stale in result.stale_files:
            if stale.env == env:
                if stale.file not in deletions:
                    plan.actions.append(FixAction(
                        ACTION_DELETE_FILE, "stale_file", stale.file))
                    deletions.add(stale.file)
            else:
                _out_of_scope(plan, "stale_files")

    want_unused = "unused" in kinds
    want_stale = "stale_params" in kinds
    if want_unused or want_stale:
        bucket = "unused" if want_unused else "stale_params"
        for finding in result.keys:
            if finding.status != "UNUSED" or finding.allowlisted:
                continue
            if not want_unused and not finding.stale_param:
                continue
            key = finding.key
            label = "stale_param" if finding.stale_param else "unused"
            if key.layer != "environment" or key.env != env:
                _out_of_scope(plan, bucket)
                continue
            _plan_removal(plan, inventory, key.file, key.name, key.line,
                          label, deletions)

    if "redundant" in kinds:
        allowlisted = {f.key.name for f in result.keys if f.allowlisted}
        for finding in result.redundant:
            scan = inventory.file_scan.get(finding.file)
            if scan is None or scan.layer != "environment" \
                    or scan.env != env:
                _out_of_scope(plan, "redundant")
                continue
            if finding.key in allowlisted:
                plan.skipped.append(FixSkip(
                    "redundant", finding.file, finding.key,
                    "key is allowlisted"))
                continue
            _plan_removal(plan, inventory, finding.file, finding.key,
                          finding.line, "redundant", deletions)

    seen = set()
    unique = []
    for action in plan.actions:
        marker = (action.action, action.file, action.start_line,
                  action.end_line)
        if marker in seen:
            continue  # e.g. a key both unused and redundant
        seen.add(marker)
        unique.append(action)
    plan.actions = unique
    return plan


def apply_fixes(plan: FixPlan, dry_run: bool = False) -> None:
    """Apply the plan; with dry_run nothing on disk changes.

    Actions that fail verification are converted to skips; afterwards
    plan.actions holds only what was actually applied.
    """
    plan.dry_run = dry_run
    if dry_run:
        return
    by_file: Dict[Path, List[FixAction]] = {}
    for action in plan.actions:
        if action.action == ACTION_REMOVE_KEY:
            by_file.setdefault(action.file, []).append(action)
    for file, actions in sorted(by_file.items()):
        _apply_removals(plan, file, actions)
    for action in plan.actions:
        if action.action == ACTION_DELETE_FILE:
            try:
                action.file.unlink()
                action.applied = True
            except OSError as exc:
                plan.errors.append("cannot delete %s: %s"
                                   % (action.file, exc))
    plan.actions = [a for a in plan.actions if a.applied]


def _out_of_scope(plan: FixPlan, kind: str) -> None:
    plan.out_of_scope[kind] = plan.out_of_scope.get(kind, 0) + 1


def _plan_removal(plan: FixPlan, inventory, file: Path, name: str,
                  line: int, finding: str, deletions: Set[Path]) -> None:
    if file in deletions:
        return  # the whole file is being deleted anyway
    doc: Optional[LoadedDoc] = inventory.docs.get(file.resolve())
    if doc is None:
        plan.skipped.append(FixSkip(
            finding, file, name, "data file was not loaded"))
        return
    if doc.flow_root:
        plan.skipped.append(FixSkip(
            finding, file, name,
            "flow-style root mapping; line-based removal is unsupported"))
        return
    top = next((t for t in doc.keys
                if t.name == name and t.line == line), None)
    if top is None:
        plan.skipped.append(FixSkip(
            finding, file, name, "definition not found in parsed file"))
        return
    if top.from_merge:
        plan.skipped.append(FixSkip(
            finding, file, name,
            "introduced by a YAML merge key; edit the anchor target "
            "instead"))
        return
    if top.node is None or top.line <= 0:
        plan.skipped.append(FixSkip(
            finding, file, name,
            "parsed via json fallback; line numbers unknown"))
        return
    if name in doc.duplicates:
        plan.skipped.append(FixSkip(
            finding, file, name,
            "duplicate top-level key; removing one occurrence changes "
            "what the other resolves to"))
        return
    if _entangled(doc, top):
        plan.skipped.append(FixSkip(
            finding, file, name,
            "value is anchored and aliased by another key"))
        return
    start, end = _key_span(top)
    plan.actions.append(FixAction(
        ACTION_REMOVE_KEY, finding, file, key=name,
        start_line=start, end_line=end))


def _key_span(top: TopKey) -> Tuple[int, int]:
    """1-based inclusive line span of a top-level key and its value.

    A block node's end mark sits at column 0 of the token that ended
    the block (the next key or EOF); that line is not part of the
    value.
    """
    mark = top.node.end_mark
    end = mark.line if mark.column > 0 else mark.line - 1  # 0-based
    return top.line, max(top.line, end + 1)


def _entangled(doc: LoadedDoc, top: TopKey) -> bool:
    """True when top's value shares nodes (anchors/aliases/merge
    targets) with another key; removing it would break the file."""
    mine = _subtree_ids(top.node)
    for other in doc.keys:
        if other is top or other.node is None:
            continue
        if mine & _subtree_ids(other.node):
            return True
    return False


def _subtree_ids(node: yaml.Node) -> Set[int]:
    seen: Set[int] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, yaml.SequenceNode):
            stack.extend(current.value)
        elif isinstance(current, yaml.MappingNode):
            for key_node, value_node in current.value:
                stack.append(key_node)
                stack.append(value_node)
    return seen


def _apply_removals(plan: FixPlan, file: Path,
                    actions: List[FixAction]) -> None:
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        plan.errors.append("cannot read %s: %s" % (file, exc))
        return
    except UnicodeDecodeError:
        plan.errors.append("cannot read %s: not valid UTF-8" % file)
        return
    lines = text.splitlines(keepends=True)
    drop: Set[int] = set()  # 0-based indexes
    written: List[FixAction] = []
    for action in sorted(actions, key=lambda a: a.start_line):
        start, end = action.start_line - 1, action.end_line - 1
        if start < 0 or end >= len(lines):
            plan.skipped.append(FixSkip(
                action.finding, file, action.key,
                "file changed since scan; line span outside file"))
            continue
        if not _line_starts_key(lines[start], action.key or ""):
            plan.skipped.append(FixSkip(
                action.finding, file, action.key,
                "file changed since scan; line %d does not define the "
                "key" % action.start_line))
            continue
        span = set(range(start, end + 1))
        if span & drop:
            plan.skipped.append(FixSkip(
                action.finding, file, action.key,
                "removal span overlaps another removal"))
            continue
        drop |= span
        written.append(action)
    if not drop:
        return
    new_text = "".join(line for index, line in enumerate(lines)
                       if index not in drop)
    try:
        file.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        plan.errors.append("cannot write %s: %s" % (file, exc))
        return
    for action in written:
        action.applied = True


def _line_starts_key(line: str, name: str) -> bool:
    """Verify a source line begins the definition of `name` at column
    0, in plain, single-quoted or double-quoted form."""
    stripped = line.rstrip("\r\n")
    candidates = [name,
                  "'%s'" % name.replace("'", "''"),
                  json.dumps(name)]
    for candidate in candidates:
        if re.match(re.escape(candidate) + r"\s*:(?:\s|$)", stripped):
            return True
    return False
