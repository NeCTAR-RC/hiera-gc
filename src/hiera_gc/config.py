from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

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
    #: Extra environments-root directories, like additional entries on
    #: Puppet's environmentpath. Each holds environment subdirectories.
    env_dirs: List[Path] = field(default_factory=list)
    extra_datadirs: List[Path] = field(default_factory=list)
    allowlist: Optional[Path] = None
    strict: bool = False
    verbosity: int = 0


def discover_environments(
        config: RunConfig,
        problems: Optional[List[str]] = None) -> List[Environment]:
    """Discover environments across the default root and any extra roots.

    This mirrors Puppet's ``environmentpath``: a root is a directory whose
    immediate subdirectories are environments. The default
    ``<code-dir>/environments`` is searched first, then each
    ``config.env_dirs`` (``--env-dir``) root in order. When an environment
    name appears under more than one root the first occurrence wins and the
    rest are reported as shadowed, matching Puppet's first-match resolution.

    Diagnostic strings (a missing ``--env-dir`` root, a shadowed name) are
    appended to ``problems`` when given; the caller turns them into
    warnings. ``--env`` and ``--env-glob`` filter across every root.
    """
    roots = [config.code_dir / "environments"] + list(config.env_dirs)

    seen_roots = set()
    found: Dict[str, Environment] = {}
    order: List[Environment] = []
    for index, root in enumerate(roots):
        explicit = index > 0  # extra --env-dir roots are user-supplied
        real = root.resolve()
        if real in seen_roots:
            continue
        seen_roots.add(real)
        if not root.is_dir():
            if explicit and problems is not None:
                problems.append("--env-dir %s does not exist" % root)
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if config.envs and child.name not in config.envs:
                continue
            if config.env_glob and not fnmatch.fnmatch(child.name,
                                                       config.env_glob):
                continue
            if child.name in found:
                if problems is not None:
                    problems.append(
                        "environment '%s' under %s is ignored; the copy "
                        "under %s is used (environmentpath order)"
                        % (child.name, child.parent,
                           found[child.name].path.parent))
                continue
            env = Environment(name=child.name, path=child)
            found[child.name] = env
            order.append(env)
    return order
