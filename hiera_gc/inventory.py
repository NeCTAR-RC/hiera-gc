"""Discovery of hiera data files and their top-level keys."""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from hiera_gc.analysis import Warn
from hiera_gc.config import DATA_EXTENSIONS, Environment, RunConfig
from hiera_gc.hiera_config import (
    HieraConfig,
    HierarchyEntry,
    parse_hiera_config,
)
from hiera_gc.yamlloc import DataFileError, LoadedDoc, load_data_file

INTERP_RE = re.compile(r"%\{[^}]*\}")
#: Absolute datadirs written for the real puppetserver get rebased under
#: --code-dir when the tool runs against a copied tree.
DEFAULT_CODE_PREFIX = "/etc/puppetlabs/code/"

LAYER_GLOBAL = "global"
LAYER_SHARED = "shared"
LAYER_ENVIRONMENT = "environment"
LAYER_MODULE = "module"


@dataclass(frozen=True)
class DataKey:
    name: str
    file: Path
    line: int
    layer: str  # global | shared | environment | module
    env: str | None  # None when visible to every environment
    module: str | None
    digest: str
    from_merge: bool = False


@dataclass
class DataFileInfo:
    file: Path
    rel: str  # posix path relative to the datadir
    datadir: Path


@dataclass
class DataDirScan:
    datadir: Path  # realpath
    layer: str
    env: str | None
    module: str | None
    entries: list[HierarchyEntry] = field(default_factory=list)
    files: list[DataFileInfo] = field(default_factory=list)


@dataclass
class Inventory:
    scans: dict[Path, DataDirScan] = field(default_factory=dict)
    keys: list[DataKey] = field(default_factory=list)
    docs: dict[Path, LoadedDoc] = field(default_factory=dict)
    file_scan: dict[Path, DataDirScan] = field(default_factory=dict)
    shared_refs: dict[Path, list[str]] = field(default_factory=dict)
    env_hiera: dict[str, HieraConfig] = field(default_factory=dict)
    global_hiera: HieraConfig | None = None
    warnings: list[Warn] = field(default_factory=list)
    files_scanned: int = 0

    def keys_by_name(self) -> dict[str, list[DataKey]]:
        grouped: dict[str, list[DataKey]] = {}
        for key in self.keys:
            grouped.setdefault(key.name, []).append(key)
        return grouped


def build_inventory(
    config: RunConfig, environments: list[Environment]
) -> Inventory:
    inv = Inventory()

    if config.global_hiera:
        if config.global_hiera.is_file():
            hiera = parse_hiera_config(config.global_hiera)
            inv.global_hiera = hiera
            inv.warnings.extend(hiera.warnings)
            if hiera.usable:
                _scan_layer(
                    inv,
                    config,
                    hiera,
                    base_dir=config.global_hiera.parent,
                    layer=LAYER_GLOBAL,
                    env=None,
                    module=None,
                )
        else:
            inv.warnings.append(
                Warn(
                    "hiera_config",
                    f"global hiera.yaml not found at {config.global_hiera} (continuing without "
                    "a global layer)",
                )
            )

    for env in environments:
        _scan_environment(inv, config, env)

    for extra in config.extra_datadirs:
        if not extra.is_dir():
            inv.warnings.append(
                Warn("hiera_config", f"--extra-datadir {extra} does not exist")
            )
            continue
        entry = HierarchyEntry(
            name="(extra-datadir)",
            datadir_raw=str(extra),
            backend_name="yaml_data",
            backend_kind="yaml",
            patterns=["**"],
            glob_flags=[True],
        )
        _register_datadir(
            inv, extra, LAYER_SHARED, env=None, module=None, entry=entry
        )
    return inv


def scan_module_data(
    inv: Inventory,
    config: RunConfig,
    module_dir: Path,
    module_name: str,
    env: str | None,
) -> None:
    """Scan a module's own hiera data (module layer), if it has any."""
    hiera_path = module_dir / "hiera.yaml"
    if not hiera_path.is_file():
        return
    hiera = parse_hiera_config(hiera_path)
    inv.warnings.extend(hiera.warnings)
    if hiera.usable:
        _scan_layer(
            inv,
            config,
            hiera,
            base_dir=module_dir,
            layer=LAYER_MODULE,
            env=env,
            module=module_name,
        )


def _scan_environment(
    inv: Inventory, config: RunConfig, env: Environment
) -> None:
    hiera_path = env.path / "hiera.yaml"
    if hiera_path.is_file():
        hiera = parse_hiera_config(hiera_path)
        inv.warnings.extend(hiera.warnings)
    else:
        hiera = HieraConfig(file=hiera_path, usable=False)
        inv.warnings.append(
            Warn(
                "hiera_config",
                f"environment '{env.name}' has no hiera.yaml; assuming default "
                "hierarchy (data/common.yaml)",
            )
        )
    if not hiera.usable:
        hiera.entries = [
            HierarchyEntry(
                name="(default hierarchy)",
                datadir_raw="data",
                backend_name="yaml_data",
                backend_kind="yaml",
                patterns=["common.yaml"],
                glob_flags=[False],
                config_file=hiera_path,
            )
        ]
    inv.env_hiera[env.name] = hiera
    _scan_layer(
        inv,
        config,
        hiera,
        base_dir=env.path,
        layer=LAYER_ENVIRONMENT,
        env=env.name,
        module=None,
    )


def _scan_layer(
    inv: Inventory,
    config: RunConfig,
    hiera: HieraConfig,
    base_dir: Path,
    layer: str,
    env: str | None,
    module: str | None,
) -> None:
    for entry in hiera.entries:
        if not entry.scannable:
            continue
        for datadir in _resolve_datadirs(inv, config, entry, base_dir):
            entry_layer = layer
            entry_env = env
            if layer == LAYER_ENVIRONMENT and env is not None:
                try:
                    datadir.relative_to(base_dir.resolve())
                except ValueError:
                    # Datadir outside the environment: shared between
                    # every environment whose hiera.yaml points at it.
                    entry_layer = LAYER_SHARED
                    entry_env = None
            _register_datadir(
                inv, datadir, entry_layer, entry_env, module, entry
            )
            if entry_layer == LAYER_SHARED and env is not None:
                refs = inv.shared_refs.setdefault(datadir, [])
                if env not in refs:
                    refs.append(env)


def _resolve_datadirs(
    inv: Inventory, config: RunConfig, entry: HierarchyEntry, base_dir: Path
) -> list[Path]:
    raw = entry.datadir_raw
    context = f"hierarchy entry '{entry.name}' in {entry.config_file}"

    if "%{" in raw:
        pattern = INTERP_RE.sub("*", raw)
        if not os.path.isabs(pattern):
            pattern = str(base_dir / pattern)
        matches = sorted(p for p in glob.glob(pattern) if os.path.isdir(p))
        if not matches:
            inv.warnings.append(
                Warn(
                    "hiera_config",
                    f"{context}: interpolated datadir {raw!r} matches no directories "
                    f"(tried {pattern})",
                )
            )
        return [Path(p).resolve() for p in matches]

    if os.path.isabs(raw):
        path = Path(raw)
        if path.is_dir():
            return [path.resolve()]
        if raw.startswith(DEFAULT_CODE_PREFIX):
            candidate = config.code_dir / raw[len(DEFAULT_CODE_PREFIX) :]
            if candidate.is_dir():
                inv.warnings.append(
                    Warn(
                        "hiera_config",
                        f"{context}: datadir {raw} not found; rebased under code dir as {candidate}",
                    )
                )
                return [candidate.resolve()]
        candidate = config.code_dir / path.name
        if candidate.is_dir():
            inv.warnings.append(
                Warn(
                    "hiera_config",
                    f"{context}: datadir {raw} not found; using {candidate} from the code dir "
                    "instead",
                )
            )
            return [candidate.resolve()]
        inv.warnings.append(
            Warn(
                "hiera_config",
                f"{context}: datadir {raw} does not exist (use "
                "--extra-datadir to point hiera-gc at a copy)",
            )
        )
        return []

    path = base_dir / raw
    if path.is_dir():
        return [path.resolve()]
    inv.warnings.append(
        Warn("hiera_config", f"{context}: datadir {path} does not exist")
    )
    return []


def _register_datadir(
    inv: Inventory,
    datadir: Path,
    layer: str,
    env: str | None,
    module: str | None,
    entry: HierarchyEntry,
) -> None:
    scan = inv.scans.get(datadir)
    if scan is None:
        scan = DataDirScan(
            datadir=datadir, layer=layer, env=env, module=module
        )
        inv.scans[datadir] = scan
        _scan_files(inv, scan)
    if not any(
        e.config_file == entry.config_file and e.index == entry.index
        for e in scan.entries
    ):
        scan.entries.append(entry)


def _scan_files(inv: Inventory, scan: DataDirScan) -> None:
    seen = set()
    for path in sorted(scan.datadir.rglob("*")):
        if path.suffix not in DATA_EXTENSIONS or not path.is_file():
            continue
        if any(
            part.startswith(".")
            for part in path.relative_to(scan.datadir).parts
        ):
            continue
        real = path.resolve()
        if real in seen:
            continue
        seen.add(real)
        rel = path.relative_to(scan.datadir).as_posix()
        scan.files.append(
            DataFileInfo(file=path, rel=rel, datadir=scan.datadir)
        )
        inv.file_scan[path] = scan
        inv.files_scanned += 1
        _load_keys(inv, scan, path, real)


def _load_keys(
    inv: Inventory, scan: DataDirScan, path: Path, real: Path
) -> None:
    doc = inv.docs.get(real)
    if doc is None:
        try:
            doc = load_data_file(path)
        except DataFileError as exc:
            inv.warnings.append(
                Warn(
                    "parse_error",
                    f"cannot parse data file: {exc.message}",
                    file=str(path),
                    line=exc.line,
                )
            )
            return
        inv.docs[real] = doc
        for problem in doc.problems:
            inv.warnings.append(Warn("data_file", problem, file=str(path)))
    for top in doc.keys:
        inv.keys.append(
            DataKey(
                name=top.name,
                file=path,
                line=top.line,
                layer=scan.layer,
                env=scan.env,
                module=scan.module,
                digest=top.digest(),
                from_merge=top.from_merge,
            )
        )
