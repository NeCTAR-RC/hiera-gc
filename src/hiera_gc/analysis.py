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
    # Populated by analyse() for the later analysis passes.
    config: object = None
    inventory: object = None
    scopes: Dict[str, "object"] = field(default_factory=dict)
    indexes: Dict[str, "object"] = field(default_factory=dict)

    @property
    def parse_errors(self) -> List[Warn]:
        return [w for w in self.warnings if w.kind == "parse_error"]

    def counts(self) -> Dict[str, int]:
        active = [k for k in self.keys
                  if not getattr(k, "allowlisted", False)]
        unused = [k for k in active if getattr(k, "status", "") == "UNUSED"]
        return {
            "keys": len(self.keys),
            "unused": len(unused),
            "possibly_used": len(
                [k for k in active
                 if getattr(k, "status", "") == "POSSIBLY_USED"]),
            "stale_params": len(
                [k for k in unused if getattr(k, "stale_param", None)]),
            "allowlisted": len(self.keys) - len(active),
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


def analyse(config: RunConfig,
            environments: List[Environment]) -> AnalysisResult:
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
        indexes[env.name] = build_consumer_index(
            scopes[env.name], cache, docs)
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

    result.stats.update({
        "environments": len(environments),
        "data_files": inventory.files_scanned,
        "data_keys": len(inventory.keys),
        "pp_files": sum(len(s.pp_files) for s in scopes.values()),
        "modules": len(scanned_modules),
    })
    return result


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
            if module_dir is None or not _is_under(scan.datadir,
                                                   module_dir):
                continue
        for info in scan.files:
            doc = inventory.docs.get(info.file.resolve())
            if doc is not None:
                docs.append((info.file, doc))
    return docs


def _consumer_warnings(indexes: Dict[str, "object"],
                       result: AnalysisResult) -> None:
    seen = set()
    for index in indexes.values():
        for consumer in index.dynamics:
            spot = (str(consumer.file), consumer.line)
            if spot not in seen:
                seen.add(spot)
                result.warnings.append(Warn(
                    "dynamic_lookup",
                    "%s with a runtime-built key; its consumption is "
                    "invisible to this analysis" % consumer.detail,
                    file=str(consumer.file), line=consumer.line))
        for consumer in index.hiera_includes:
            spot = ("hiera_include", str(consumer.file), consumer.line)
            if spot not in seen:
                seen.add(spot)
                result.warnings.append(Warn(
                    "hiera_include",
                    "hiera_include('%s') assigns classes from data; "
                    "class names listed under that key are consumers "
                    "this analysis does not follow" % consumer.key,
                    file=str(consumer.file), line=consumer.line))
