"""Redundant-override and shadowed-definition analysis.

Within an environment, hierarchy levels are ordered: global layer,
then the environment hiera.yaml's entries in order (shared datadirs
take the position where they are referenced), then module data. Values
are compared by digest only; the report never contains values.

- REDUNDANT: a definition equal to the next always-loaded definition
  below it (no intermediate level defining a different value): the
  higher-priority copy can be removed.
- SHADOWED: a definition that can never win because an always-loaded
  higher-priority level defines a different value.

Keys consumed by merging lookups (hiera_array/hiera_hash/hiera_include,
lookup with a merge, or lookup_options merge strategies) are excluded:
every level contributes to a merge.
"""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
from pathlib import Path
import re

from hiera_gc.analysis import AnalysisResult
from hiera_gc.checks import INTERP, hiera_pattern_to_regex
from hiera_gc.classify import visible_envs
from hiera_gc.consumers.index import ConsumerIndex
from hiera_gc.inventory import DataKey


@dataclass
class RedundantFinding:
    key: str
    file: Path
    line: int
    anchor_file: Path
    anchor_line: int
    envs: list[str]


@dataclass
class ShadowedFinding:
    key: str
    file: Path
    line: int
    shadow_file: Path
    shadow_line: int
    envs: list[str]


@dataclass
class _Def:
    prio: tuple[int, int, int]
    key: DataKey
    always: bool  # loaded for every node (interpolation-free pattern)


def run_redundancy(result: AnalysisResult) -> None:
    inventory = result.inventory
    indexes: dict[str, ConsumerIndex] = result.indexes

    file_keys: dict[Path, list[DataKey]] = {}
    for key in inventory.keys:
        file_keys.setdefault(key.file, []).append(key)

    entry_scans = {}
    for scan in inventory.scans.values():
        for entry in scan.entries:
            entry_scans.setdefault(
                (entry.config_file, entry.index), []
            ).append(scan)

    redundant_votes: dict[DataKey, dict[str, tuple[Path, int]]] = {}
    shadowed_votes: dict[DataKey, dict[str, tuple[Path, int]]] = {}
    merge_blocked = set()

    for env, index in indexes.items():
        defs_by_key = _collect_definitions(
            env, inventory, result.scopes, entry_scans, file_keys
        )
        for name, defs in defs_by_key.items():
            if len(defs) < 2:
                continue
            if _merge_excluded(name, index):
                merge_blocked.update(d.key for d in defs)
                continue
            _evaluate(env, defs, redundant_votes, shadowed_votes)

    _aggregate(result, redundant_votes, shadowed_votes, merge_blocked)


def _collect_definitions(
    env: str, inventory, scopes, entry_scans, file_keys
) -> dict[str, list[_Def]]:
    raw: dict[DataKey, list[tuple[tuple[int, int, int], bool]]] = {}
    for prio, entry, regex, always in _levels(env, inventory, scopes):
        for scan in entry_scans.get((entry.config_file, entry.index), ()):
            for info in scan.files:
                if not regex.fullmatch(info.rel):
                    continue
                for key in file_keys.get(info.file, ()):
                    raw.setdefault(key, []).append((prio, always))

    defs_by_key: dict[str, list[_Def]] = {}
    for key, matches in raw.items():
        prio = min(m[0] for m in matches)
        always = any(m[1] for m in matches)
        defs_by_key.setdefault(key.name, []).append(
            _Def(prio=prio, key=key, always=always)
        )
    for defs in defs_by_key.values():
        defs.sort(key=lambda d: d.prio)
    return defs_by_key


def _levels(env: str, inventory, scopes):
    """Yield (priority, entry, compiled_regex, always_loaded) in
    decreasing priority order for this environment."""
    tiers = []
    if inventory.global_hiera is not None and inventory.global_hiera.usable:
        tiers.append((0, inventory.global_hiera.entries))
    env_hiera = inventory.env_hiera.get(env)
    if env_hiera is not None:
        tiers.append((1, env_hiera.entries))
    module_entries = []
    scope = scopes.get(env)
    for scan in inventory.scans.values():
        if scan.layer != "module" or scope is None:
            continue
        module_dir = scope.modules.get(scan.module or "")
        if module_dir is None:
            continue
        for entry in scan.entries:
            if entry not in module_entries:
                module_entries.append(entry)
    tiers.append((2, module_entries))

    for tier, entries in tiers:
        for entry in entries:
            for pattern_index, (pattern, is_glob) in enumerate(
                zip(entry.patterns, entry.glob_flags)
            ):
                regex = re.compile(hiera_pattern_to_regex(pattern, is_glob))
                always = not INTERP.search(pattern)
                yield (
                    (tier, entry.index, pattern_index),
                    entry,
                    regex,
                    always,
                )


def _merge_excluded(name: str, index: ConsumerIndex) -> bool:
    if name == "lookup_options":
        return True  # hiera hash-merges lookup_options across levels
    for consumer in index.exact.get(name, ()):
        if consumer.merge:
            return True
    for consumer in index.dig.get(name, ()):
        if consumer.merge:
            return True
    for consumer in index.patterns:
        if consumer.merge and fnmatch.fnmatchcase(
            name, consumer.pattern or ""
        ):
            return True
    for entry in index.lookup_options:
        if entry.merge in (None, "first"):
            continue
        if entry.regex:
            try:
                if re.match(entry.name, name):
                    return True
            except re.error:
                continue
        elif entry.name == name:
            return True
    return False


def _evaluate(
    env: str, defs: list[_Def], redundant_votes, shadowed_votes
) -> None:
    for position, definition in enumerate(defs):
        below = defs[position + 1 :]
        anchor = _redundant_anchor(definition, below)
        if anchor is not None:
            redundant_votes.setdefault(definition.key, {})[env] = (
                anchor.key.file,
                anchor.key.line,
            )
            continue
        shadow = _shadowed_by(definition, defs[:position])
        if shadow is not None:
            shadowed_votes.setdefault(definition.key, {})[env] = (
                shadow.key.file,
                shadow.key.line,
            )


def _redundant_anchor(definition: _Def, below: list[_Def]) -> _Def | None:
    for candidate in below:
        if candidate.key.digest != definition.key.digest:
            return None  # an intermediate level overrides differently
        if candidate.always:
            return candidate
    return None


def _shadowed_by(definition: _Def, above: list[_Def]) -> _Def | None:
    for candidate in above:
        if candidate.always and candidate.key.digest != definition.key.digest:
            return candidate
    return None


def _aggregate(
    result: AnalysisResult, redundant_votes, shadowed_votes, merge_blocked
) -> None:
    inventory = result.inventory
    scopes = result.scopes
    for key, votes in sorted(
        redundant_votes.items(), key=lambda kv: (kv[0].name, str(kv[0].file))
    ):
        if key in merge_blocked:
            continue
        envs = visible_envs(key, inventory, scopes)
        if envs and set(envs) <= set(votes):
            anchor_file, anchor_line = votes[envs[0]]
            result.redundant.append(
                RedundantFinding(
                    key=key.name,
                    file=key.file,
                    line=key.line,
                    anchor_file=anchor_file,
                    anchor_line=anchor_line,
                    envs=envs,
                )
            )
    for key, votes in sorted(
        shadowed_votes.items(), key=lambda kv: (kv[0].name, str(kv[0].file))
    ):
        if key in merge_blocked or key in redundant_votes:
            continue
        envs = visible_envs(key, inventory, scopes)
        if envs and set(envs) <= set(votes):
            shadow_file, shadow_line = votes[envs[0]]
            result.shadowed.append(
                ShadowedFinding(
                    key=key.name,
                    file=key.file,
                    line=key.line,
                    shadow_file=shadow_file,
                    shadow_line=shadow_line,
                    envs=envs,
                )
            )
