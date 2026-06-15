"""Parsing of hiera.yaml version 5 files (global, environment and module
layers)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

from hiera_gc.analysis import Warn

# data_hash/lookup_key backend names we know how to read from disk,
# mapped to the parser family used for their files.
BACKEND_KINDS = {
    "yaml_data": "yaml",
    "eyaml_lookup_key": "yaml",  # eyaml is YAML with opaque ENC[...] values
    "json_data": "json",
    "hocon_data": "hocon",  # recognised but unsupported (warned, skipped)
}


@dataclass
class HierarchyEntry:
    name: str
    datadir_raw: str
    backend_name: str
    backend_kind: str  # yaml | json | hocon | unknown
    patterns: List[str] = field(default_factory=list)
    glob_flags: List[bool] = field(default_factory=list)  # per pattern
    config_file: Path = Path()
    index: int = 0
    default_hierarchy: bool = False

    @property
    def scannable(self) -> bool:
        return self.backend_kind in ("yaml", "json")


@dataclass
class HieraConfig:
    file: Path
    entries: List[HierarchyEntry] = field(default_factory=list)
    warnings: List[Warn] = field(default_factory=list)
    usable: bool = True


def parse_hiera_config(path: Path) -> HieraConfig:
    config = HieraConfig(file=path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8",
                                            errors="replace"))
    except yaml.YAMLError as exc:
        config.warnings.append(Warn(
            "parse_error", "cannot parse %s: %s"
            % (path, str(exc).replace("\n", " ")), file=str(path)))
        config.usable = False
        return config

    if not isinstance(raw, dict):
        config.warnings.append(Warn(
            "hiera_config", "%s is not a mapping; skipped" % path,
            file=str(path)))
        config.usable = False
        return config

    if any(str(k).startswith(":") for k in raw):
        config.warnings.append(Warn(
            "hiera_config",
            "%s looks like hiera 3 syntax (':backends:'); only hiera 5 "
            "is supported, skipped" % path, file=str(path)))
        config.usable = False
        return config

    version = raw.get("version")
    if version != 5:
        config.warnings.append(Warn(
            "hiera_config", "%s has version %r, expected 5; skipped"
            % (path, version), file=str(path)))
        config.usable = False
        return config

    defaults = raw.get("defaults") or {}
    default_datadir = str(defaults.get("datadir", "data"))
    default_backend = _backend_of(defaults) or "yaml_data"

    for section, is_default_hierarchy in (("hierarchy", False),
                                          ("default_hierarchy", True)):
        items = raw.get(section) or []
        if not isinstance(items, list):
            config.warnings.append(Warn(
                "hiera_config", "%s: %s is not a list; skipped"
                % (path, section), file=str(path)))
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            entry = _parse_entry(item, path, default_datadir,
                                 default_backend, len(config.entries),
                                 is_default_hierarchy, config.warnings)
            config.entries.append(entry)
    return config


def _backend_of(mapping: dict) -> Optional[str]:
    for key in ("data_hash", "lookup_key", "data_dig"):
        if key in mapping:
            return str(mapping[key])
    return None


def _parse_entry(item: dict, path: Path, default_datadir: str,
                 default_backend: str, index: int,
                 is_default_hierarchy: bool,
                 warnings: List[Warn]) -> HierarchyEntry:
    name = str(item.get("name", "entry %d" % index))
    backend_name = _backend_of(item) or default_backend
    backend_kind = BACKEND_KINDS.get(backend_name, "unknown")
    if backend_kind == "hocon":
        warnings.append(Warn(
            "unknown_backend",
            "hierarchy entry '%s' in %s uses hocon_data, which hiera-gc "
            "cannot read; its keys are invisible to this analysis"
            % (name, path), file=str(path)))
    elif backend_kind == "unknown":
        warnings.append(Warn(
            "unknown_backend",
            "hierarchy entry '%s' in %s uses backend '%s'; lookups served "
            "by it are invisible to this analysis"
            % (name, path, backend_name), file=str(path)))

    entry = HierarchyEntry(
        name=name,
        datadir_raw=str(item.get("datadir", default_datadir)),
        backend_name=backend_name,
        backend_kind=backend_kind,
        config_file=path,
        index=index,
        default_hierarchy=is_default_hierarchy,
    )

    def add(value: object, is_glob: bool) -> None:
        if isinstance(value, str):
            entry.patterns.append(value)
            entry.glob_flags.append(is_glob)
        elif isinstance(value, list):
            for element in value:
                if isinstance(element, str):
                    entry.patterns.append(element)
                    entry.glob_flags.append(is_glob)

    add(item.get("path"), False)
    add(item.get("paths"), False)
    add(item.get("glob"), True)
    add(item.get("globs"), True)
    mapped = item.get("mapped_paths")
    if isinstance(mapped, list) and len(mapped) == 3 \
            and isinstance(mapped[2], str):
        add(mapped[2], False)

    if not entry.patterns and entry.backend_kind in ("yaml", "json"):
        warnings.append(Warn(
            "hiera_config",
            "hierarchy entry '%s' in %s has no path/paths/glob/globs; "
            "nothing to scan" % (name, path), file=str(path)))
    return entry
