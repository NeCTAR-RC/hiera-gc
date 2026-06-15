"""Classification of every data key definition against the consumer
indexes of the environments that can see it."""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
from pathlib import Path
import re

from hiera_gc.consumers.index import ConsumerIndex
from hiera_gc.consumers.model import STRONG_KINDS
from hiera_gc.inventory import DataKey, Inventory
from hiera_gc.scope import EnvScope

USED = "USED"
POSSIBLY_USED = "POSSIBLY_USED"
UNUSED = "UNUSED"

#: Keys hiera itself consumes.
BUILTIN_KEYS = {"lookup_options"}


@dataclass
class Reason:
    kind: str
    file: str
    line: int
    detail: str = ""

    def location(self) -> str:
        return f"{self.file}:{self.line}" if self.line else self.file


@dataclass
class KeyFinding:
    key: DataKey
    status: str  # USED | POSSIBLY_USED | UNUSED
    reason: Reason | None
    envs: list[str]  # environments the definition was evaluated against
    stale_param: str | None = None
    define_shape: str | None = None
    allowlisted: bool = False


def visible_envs(
    key: DataKey, inventory: Inventory, scopes: dict[str, EnvScope]
) -> list[str]:
    all_envs = sorted(scopes)
    if key.layer == "environment":
        return [key.env] if key.env in scopes else []
    if key.layer == "shared":
        scan = inventory.file_scan.get(key.file)
        refs = inventory.shared_refs.get(scan.datadir) if scan else None
        return sorted(set(refs) & set(scopes)) if refs else all_envs
    if key.layer == "module":
        scan = inventory.file_scan.get(key.file)
        if scan is None or scan.module is None:
            return all_envs
        result = []
        for env_name, scope in scopes.items():
            module_dir = scope.modules.get(scan.module)
            if module_dir is not None and _is_under(scan.datadir, module_dir):
                result.append(env_name)
        return sorted(result)
    return all_envs  # global layer


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def classify_all(
    inventory: Inventory,
    scopes: dict[str, EnvScope],
    indexes: dict[str, ConsumerIndex],
    allowlist,
) -> list[KeyFinding]:
    from hiera_gc.allowlist import is_allowlisted

    definitions = _definition_locations(inventory)
    findings = []
    for key in inventory.keys:
        envs = visible_envs(key, inventory, scopes)
        finding = _classify_key(key, envs, indexes, definitions)
        finding.allowlisted = is_allowlisted(key.name, allowlist)
        findings.append(finding)
    return findings


def _definition_locations(
    inventory: Inventory,
) -> dict[str, set[tuple[str, int]]]:
    locations: dict[str, set[tuple[str, int]]] = {}
    for key in inventory.keys:
        locations.setdefault(key.name, set()).add((str(key.file), key.line))
    return locations


def _classify_key(
    key: DataKey,
    envs: list[str],
    indexes: dict[str, ConsumerIndex],
    definitions: dict[str, set[tuple[str, int]]],
) -> KeyFinding:
    name = key.name
    if name in BUILTIN_KEYS:
        return KeyFinding(
            key=key,
            status=USED,
            envs=envs,
            reason=Reason(
                kind="builtin",
                file=str(key.file),
                line=key.line,
                detail="consumed by hiera itself",
            ),
        )

    weak_reason: Reason | None = None
    stale_param: str | None = None
    define_shape: str | None = None

    for env in envs:
        index = indexes.get(env)
        if index is None:
            continue
        strong = _strong_reason(name, index)
        if strong is not None:
            return KeyFinding(key=key, status=USED, reason=strong, envs=envs)
        if weak_reason is None:
            weak_reason = _weak_reason(name, index, definitions)
        if stale_param is None:
            stale_param = _stale_param(name, index)
        if define_shape is None:
            define_shape = _define_shape(name, index)

    if weak_reason is not None:
        return KeyFinding(
            key=key,
            status=POSSIBLY_USED,
            reason=weak_reason,
            envs=envs,
            stale_param=stale_param,
            define_shape=define_shape,
        )
    return KeyFinding(
        key=key,
        status=UNUSED,
        reason=None,
        envs=envs,
        stale_param=stale_param,
        define_shape=define_shape,
    )


def _strong_reason(name: str, index: ConsumerIndex) -> Reason | None:
    for consumer in index.exact.get(name, ()):
        if consumer.kind in STRONG_KINDS:
            return Reason(
                kind=consumer.kind,
                file=str(consumer.file),
                line=consumer.line,
                detail=consumer.detail,
            )
    for consumer in index.dig.get(name, ()):
        return Reason(
            kind=consumer.kind,
            file=str(consumer.file),
            line=consumer.line,
            detail=f"{consumer.detail} dotted lookup '{consumer.key}'",
        )
    apl = _apl_reason(name, index)
    if apl is not None:
        return apl
    return None


def _apl_reason(name: str, index: ConsumerIndex) -> Reason | None:
    if "::" not in name:
        return None
    class_name, _, param = name.rpartition("::")
    if not class_name or not param:
        return None
    entry = index.classes.get(class_name)
    if entry is None:
        return None
    class_def, file = entry
    for param_def in class_def.params:
        if param_def.name == param:
            return Reason(
                kind="apl",
                file=str(file),
                line=param_def.line,
                detail=f"class {class_name} parameter ${param}",
            )
    return None


def _weak_reason(
    name: str,
    index: ConsumerIndex,
    definitions: dict[str, set[tuple[str, int]]],
) -> Reason | None:
    for consumer in index.exact.get(name, ()):
        if consumer.kind not in STRONG_KINDS:
            return Reason(
                kind=consumer.kind,
                file=str(consumer.file),
                line=consumer.line,
                detail=consumer.detail,
            )
    for consumer in index.dotted_full.get(name, ()):
        return Reason(
            kind="dotted_ambiguity",
            file=str(consumer.file),
            line=consumer.line,
            detail="lookup('{}') digs into a top-level key "
            "'{}' but may target this literal key".format(
                consumer.key, name.split(".", 1)[0]
            ),
        )
    for consumer in index.patterns:
        if fnmatch.fnmatchcase(name, consumer.pattern or ""):
            return Reason(
                kind="dynamic_pattern",
                file=str(consumer.file),
                line=consumer.line,
                detail=f"matches {consumer.detail} key pattern {consumer.pattern!r}",
            )
    for entry in index.lookup_options:
        if (entry.regex and _regex_match(entry.name, name)) or (
            not entry.regex and entry.name == name
        ):
            return Reason(
                kind="lookup_options_ref",
                file=str(entry.file),
                line=entry.line,
                detail="referenced by lookup_options",
            )
    own = definitions.get(name, set())
    for file, line in index.mentions.get(name, ()):
        if (str(file), line) not in own:
            return Reason(
                kind="mention",
                file=str(file),
                line=line,
                detail="key name appears verbatim",
            )
    return None


def _regex_match(pattern: str, name: str) -> bool:
    try:
        return re.match(pattern, name) is not None
    except re.error:
        return False


def _stale_param(name: str, index: ConsumerIndex) -> str | None:
    if "::" not in name:
        return None
    class_name, _, param = name.rpartition("::")
    entry = index.classes.get(class_name)
    if entry is None:
        return None
    class_def, file = entry
    if any(p.name == param for p in class_def.params):
        return None
    return (
        f"class {class_name} exists ({file}:{class_def.line}) "
        f"but has no parameter ${param}"
    )


def _define_shape(name: str, index: ConsumerIndex) -> str | None:
    if "::" not in name:
        return None
    define_name, _, param = name.rpartition("::")
    entry = index.defines.get(define_name)
    if entry is None:
        return None
    define_def, file = entry
    if any(p.name == param for p in define_def.params):
        return (
            f"matches defined type {define_name} "
            f"({file}:{define_def.line}), which does not use "
            "automatic parameter lookup"
        )
    return None
