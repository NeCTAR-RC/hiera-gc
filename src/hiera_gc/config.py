from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

DEFAULT_CODE_DIR = Path("/etc/puppetlabs/code")
DEFAULT_GLOBAL_HIERA = Path("/etc/puppetlabs/puppet/hiera.yaml")

#: File extensions considered hiera data; anything else in a datadir
#: (e.g. hiera-eyaml-gpg.recipients) is ignored.
DATA_EXTENSIONS = (".yaml", ".yml", ".eyaml", ".json")


@dataclass(frozen=True)
class Environment:
    name: str
    path: Path


@dataclass
class RunConfig:
    code_dir: Path = DEFAULT_CODE_DIR
    global_hiera: Optional[Path] = DEFAULT_GLOBAL_HIERA
    envs: List[str] = field(default_factory=list)
    env_glob: Optional[str] = None
    extra_datadirs: List[Path] = field(default_factory=list)
    allowlist: Optional[Path] = None
    strict: bool = False
    verbosity: int = 0


def discover_environments(config: RunConfig) -> List[Environment]:
    env_root = config.code_dir / "environments"
    if not env_root.is_dir():
        return []
    found = []
    for child in sorted(env_root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if config.envs and child.name not in config.envs:
            continue
        if config.env_glob and not fnmatch.fnmatch(child.name, config.env_glob):
            continue
        found.append(Environment(name=child.name, path=child))
    return found
