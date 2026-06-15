"""Per-environment file sets: which manifests, modules, templates and
plugins are visible to an environment's compiler."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from hiera_gc.analysis import Warn
from hiera_gc.config import Environment, RunConfig

MODULE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
#: Directories whose contents are not part of the compiled catalog.
EXCLUDED_DIRS = {
    "spec",
    "examples",
    "tests",
    "pkg",
    "vendor",
    ".git",
    ".librarian",
    "fixtures",
}
VENDOR_MODULE_PATH = Path("/opt/puppetlabs/puppet/modules")


@dataclass
class EnvScope:
    env: Environment
    modules: dict[str, Path] = field(default_factory=dict)
    pp_files: list[Path] = field(default_factory=list)
    epp_files: list[Path] = field(default_factory=list)
    erb_files: list[Path] = field(default_factory=list)
    ruby_files: list[Path] = field(default_factory=list)
    warnings: list[Warn] = field(default_factory=list)


def build_scope(env: Environment, config: RunConfig) -> EnvScope:
    scope = EnvScope(env=env)

    for directory in _modulepath(env, config, scope.warnings):
        if not directory.is_dir():
            continue
        for child in sorted(directory.iterdir()):
            if (
                child.is_dir()
                and MODULE_NAME_RE.match(child.name)
                and child.name not in scope.modules
            ):
                scope.modules[child.name] = child

    scope.pp_files.extend(_walk(env.path / "manifests", (".pp",)))
    scope.pp_files.extend(_walk(env.path / "functions", (".pp",)))
    scope.ruby_files.extend(_walk(env.path / "lib" / "puppet", (".rb",)))

    for module_dir in scope.modules.values():
        scope.pp_files.extend(_walk(module_dir / "manifests", (".pp",)))
        scope.pp_files.extend(_walk(module_dir / "functions", (".pp",)))
        templates = list(_walk(module_dir / "templates", (".epp", ".erb")))
        scope.epp_files.extend(p for p in templates if p.suffix == ".epp")
        scope.erb_files.extend(p for p in templates if p.suffix == ".erb")
        scope.ruby_files.extend(_walk(module_dir / "lib" / "puppet", (".rb",)))
    return scope


def _modulepath(
    env: Environment, config: RunConfig, warnings: list[Warn]
) -> list[Path]:
    base = [config.code_dir / "modules"]
    if VENDOR_MODULE_PATH.is_dir():
        base.append(VENDOR_MODULE_PATH)

    raw = _environment_conf_value(
        env.path / "environment.conf", "modulepath", warnings
    )
    if raw is None:
        return [env.path / "modules"] + base

    result: list[Path] = []
    for part in raw.split(":"):
        part = part.strip()
        if not part:
            continue
        if part == "$basemodulepath":
            result.extend(base)
        elif os.path.isabs(part):
            result.append(Path(part))
        else:
            result.append(env.path / part)
    return result


def _environment_conf_value(path: Path, wanted: str, warnings: list[Warn]):
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        warnings.append(
            Warn("parse_error", f"cannot read {path}: {exc}", file=str(path))
        )
        return None
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == wanted:
            return value.strip().strip("'\"")
    return None


def _walk(root: Path, suffixes) -> list[Path]:
    if not root.is_dir():
        return []
    found = []
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [
            d
            for d in sorted(dirnames)
            if d not in EXCLUDED_DIRS and not d.startswith(".")
        ]
        for filename in sorted(filenames):
            if filename.endswith(tuple(suffixes)):
                found.append(Path(dirpath) / filename)
    return found
