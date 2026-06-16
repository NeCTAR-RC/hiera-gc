from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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
        return f"{self.file}:{self.line}" if self.line else self.file


@dataclass
class AnalysisResult:
    environments: list[str] = field(default_factory=list)
    keys: list[object] = field(default_factory=list)
    orphans: list[object] = field(default_factory=list)
    stale_files: list[object] = field(default_factory=list)
    redundant: list[object] = field(default_factory=list)
    shadowed: list[object] = field(default_factory=list)
    warnings: list[Warn] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    # Populated by analyse() for the later analysis passes.
    config: object = None
    inventory: object = None
    scopes: dict[str, object] = field(default_factory=dict)
    indexes: dict[str, object] = field(default_factory=dict)
    # Set by restrict_to_environment() when a run is narrowed to a
    # single environment: the environment name, and the per-section
    # count of findings hidden because they live in shared/global/module
    # data (which other, unanalysed environments may consume or own).
    restricted_env: str | None = None
    restricted_suppressed: dict[str, int] = field(default_factory=dict)

    @property
    def parse_errors(self) -> list[Warn]:
        return [w for w in self.warnings if w.kind == "parse_error"]

    def counts(self) -> dict[str, int]:
        active = [k for k in self.keys if not getattr(k, "allowlisted", False)]
        unused = [k for k in active if getattr(k, "status", "") == "UNUSED"]
        return {
            "keys": len(self.keys),
            "unused": len(unused),
            "possibly_used": len(
                [
                    k
                    for k in active
                    if getattr(k, "status", "") == "POSSIBLY_USED"
                ]
            ),
            "stale_params": len(
                [k for k in unused if getattr(k, "stale_param", None)]
            ),
            "allowlisted": len(self.keys) - len(active),
            "orphans": len(self.orphans),
            "stale_files": len(self.stale_files),
            "redundant": len(self.redundant),
            "shadowed": len(self.shadowed),
            "warnings": len(self.warnings),
        }

    def fails(self, fail_on: list[str]) -> bool:
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
        parts = [f"{k}={v}" for k, v in sorted(self.stats.items())]
        return "hiera-gc stats: " + " ".join(parts)


def analyse(
    config: RunConfig, environments: list[Environment]
) -> AnalysisResult:
    from hiera_gc.allowlist import load_allowlist
    from hiera_gc.classify import classify_all
    from hiera_gc.consumers.index import build_consumer_index
    from hiera_gc.filecache import ExtractorCache
    from hiera_gc.inventory import build_inventory, scan_module_data
    from hiera_gc.scope import build_scope

    result = AnalysisResult(environments=[e.name for e in environments])
    result.config = config
    inventory = build_inventory(config, environments)
    cache = ExtractorCache()

    scopes = {}
    scanned_modules = set()
    for env in environments:
        scope = build_scope(env, config)
        scopes[env.name] = scope
        result.warnings.extend(scope.warnings)
        for name, module_dir in scope.modules.items():
            real = module_dir.resolve()
            if real in scanned_modules:
                continue
            scanned_modules.add(real)
            env_name = env.name if _is_under(module_dir, env.path) else None
            scan_module_data(inventory, config, module_dir, name, env_name)
    result.warnings.extend(inventory.warnings)

    indexes = {}
    for env in environments:
        docs = _visible_docs(env.name, inventory, scopes)
        indexes[env.name] = build_consumer_index(scopes[env.name], cache, docs)
    result.warnings.extend(cache.warnings)
    _consumer_warnings(indexes, result)

    allowlist = []
    if config.allowlist is not None:
        allowlist = load_allowlist(config.allowlist, result.warnings)
    result.keys = classify_all(inventory, scopes, indexes, allowlist)

    result.inventory = inventory
    result.scopes = scopes
    result.indexes = indexes

    from hiera_gc.checks import run_checks
    from hiera_gc.redundancy import run_redundancy

    run_checks(result)
    run_redundancy(result)

    result.stats.update(
        {
            "environments": len(environments),
            "data_files": inventory.files_scanned,
            "data_keys": len(inventory.keys),
            "pp_files": sum(len(s.pp_files) for s in scopes.values()),
            "modules": len(scanned_modules),
        }
    )
    return result


#: Disjoint report sections whose suppression is tallied for the note;
#: stale_params is omitted because it is a subset of unused.
_RESTRICT_SECTIONS = (
    "unused",
    "possibly_used",
    "orphans",
    "stale_files",
    "redundant",
    "shadowed",
)


def restrict_to_environment(
    result: AnalysisResult, env: str
) -> dict[str, int]:
    """Trim findings to those that live in ``env``'s own environment-layer
    data.

    A run narrowed to a single environment should report and fail on only
    what can be acted on inside that one environment, matching what
    ``--fix`` would touch. Shared, global and module layer findings are
    visible to other environments that this run did not analyse, so
    reporting them here would be both unfixable from this environment's
    repository and unreliable (another environment may consume the key).
    Those findings are surfaced by an all-environments run instead.

    Mutates ``result`` in place, records the restriction on it and returns
    the per-section count of findings removed.
    """
    inventory = result.inventory
    scan_by_path: dict[str, object] = {}
    if inventory is not None:
        for path, scan in inventory.file_scan.items():
            scan_by_path[str(path)] = scan
            scan_by_path[str(path.resolve())] = scan

    def in_env_data(file) -> bool:
        scan = scan_by_path.get(str(file))
        return (
            scan is not None
            and scan.layer == "environment"
            and scan.env == env
        )

    before = result.counts()
    result.keys = [
        f
        for f in result.keys
        if f.key.layer == "environment" and f.key.env == env
    ]
    result.orphans = [
        o for o in result.orphans if o.layer == "environment" and o.env == env
    ]
    result.stale_files = [s for s in result.stale_files if s.env == env]
    result.redundant = [r for r in result.redundant if in_env_data(r.file)]
    result.shadowed = [s for s in result.shadowed if in_env_data(s.file)]
    scope = result.scopes.get(env)
    result.warnings = [w for w in result.warnings if _warning_in_env(w, scope)]
    after = result.counts()

    suppressed = {
        section: before[section] - after[section]
        for section in _RESTRICT_SECTIONS
        if before[section] - after[section] > 0
    }
    result.restricted_env = env
    result.restricted_suppressed = suppressed
    return suppressed


def _warning_in_env(warn: Warn, scope) -> bool:
    """Whether a warning belongs to the restricted environment's own tree.

    A warning is kept when its file lives directly under the environment's
    directory and not inside one of its modules: that is the environment's
    own data, manifests, templates and config. Warnings about shared,
    global or module files (a module's hiera.yaml, a ``lookup()`` in a
    module manifest, an empty module data file) are dropped; an
    all-environments run reports them.

    Parse errors are kept regardless of location because a file the
    analyser could not read blinds every environment that can see it, and
    ``--strict`` / ``--fix`` depend on seeing them. Warnings with no file
    (a missing global hiera.yaml, a skipped stale-file check) are general
    diagnostics and kept. If the scope is unavailable the warning is kept.
    """
    if warn.kind == "parse_error" or not warn.file:
        return True
    if scope is None:
        return True
    file = Path(warn.file)
    if not _is_under(file, scope.env.path):
        return False  # shared, global or another environment's tree
    # Under the environment directory, but a site module placed there
    # (e.g. <env>/modules/<name>) is module layer, not the env's own.
    return not any(
        _is_under(file, module_dir) for module_dir in scope.modules.values()
    )


def _is_under(path, root) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _visible_docs(env_name: str, inventory, scopes) -> list:
    docs = []
    scope = scopes[env_name]
    for scan in inventory.scans.values():
        if scan.layer == "environment":
            if scan.env != env_name:
                continue
        elif scan.layer == "shared":
            refs = inventory.shared_refs.get(scan.datadir)
            if refs and env_name not in refs:
                continue
        elif scan.layer == "module":
            module_dir = scope.modules.get(scan.module or "")
            if module_dir is None or not _is_under(scan.datadir, module_dir):
                continue
        for info in scan.files:
            doc = inventory.docs.get(info.file.resolve())
            if doc is not None:
                docs.append((info.file, doc))
    return docs


def _consumer_warnings(
    indexes: dict[str, object], result: AnalysisResult
) -> None:
    seen = set()
    for index in indexes.values():
        for consumer in index.dynamics:
            spot = (str(consumer.file), consumer.line)
            if spot not in seen:
                seen.add(spot)
                result.warnings.append(
                    Warn(
                        "dynamic_lookup",
                        f"{consumer.detail} with a runtime-built key; its consumption is "
                        "invisible to this analysis",
                        file=str(consumer.file),
                        line=consumer.line,
                    )
                )
        for consumer in index.hiera_includes:
            spot = ("hiera_include", str(consumer.file), consumer.line)
            if spot not in seen:
                seen.add(spot)
                result.warnings.append(
                    Warn(
                        "hiera_include",
                        f"hiera_include('{consumer.key}') assigns classes from data; "
                        "class names listed under that key are consumers "
                        "this analysis does not follow",
                        file=str(consumer.file),
                        line=consumer.line,
                    )
                )
