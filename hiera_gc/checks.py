"""File-level findings: orphaned data files (unreachable via any
hierarchy pattern), stale data files (group/node files whose
interpolation variable can never take that value) and stale
lookup_options entries."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from hiera_gc.analysis import AnalysisResult, Warn

INTERP = re.compile(r"%\{([^}]*)\}")
BARE_VAR = re.compile(r"^\w+$")
#: Interpolation variables that hold the node's certname/fqdn.
NODE_NAME_VARS = {
    "trusted.certname",
    "facts.fqdn",
    "facts.networking.fqdn",
    "fqdn",
    "::fqdn",
    "clientcert",
    "::clientcert",
    "trusted.hostname",
}


@dataclass
class OrphanFile:
    file: Path
    datadir: Path
    layer: str
    env: str | None
    message: str = "matches no hierarchy path or glob"


@dataclass
class StaleFile:
    file: Path
    env: str
    message: str


def hiera_pattern_to_regex(
    pattern: str, is_glob: bool, capture_interp: bool = False
) -> str:
    """Translate a hierarchy path/glob (with %{...} interpolations) to a
    regex over datadir-relative paths. With capture_interp, the single
    interpolation becomes a capture group."""
    out = []
    i = 0
    for match in INTERP.finditer(pattern):
        out.append(_glob_part(pattern[i : match.start()], is_glob))
        out.append("(.*)" if capture_interp else ".*")
        i = match.end()
    out.append(_glob_part(pattern[i:], is_glob))
    return "".join(out)


def _glob_part(text: str, is_glob: bool) -> str:
    if not is_glob:
        return re.escape(text)
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if text.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            end = text.find("]", i + 1)
            if end == -1:
                out.append(re.escape(ch))
                i += 1
            else:
                out.append(text[i : end + 1])
                i = end + 1
        elif ch == "{":
            end = text.find("}", i + 1)
            if end == -1:
                out.append(re.escape(ch))
                i += 1
            else:
                alternatives = text[i + 1 : end].split(",")
                out.append(
                    "(?:{})".format(
                        "|".join(re.escape(a) for a in alternatives)
                    )
                )
                i = end + 1
        else:
            out.append(re.escape(ch))
            i += 1
    return "".join(out)


@dataclass
class _CompiledPattern:
    pattern: str
    regex: re.Pattern
    capture: re.Pattern | None
    interps: list[str]
    env: str | None  # environment owning the hiera.yaml entry


def run_checks(result: AnalysisResult) -> None:
    inventory = result.inventory
    indexes = result.indexes
    config_env = {
        hiera.file: name for name, hiera in inventory.env_hiera.items()
    }
    skipped_vars = set()

    for scan in inventory.scans.values():
        compiled = _compile_patterns(scan, config_env)
        for info in scan.files:
            matches = [c for c in compiled if c.regex.fullmatch(info.rel)]
            if not matches:
                result.orphans.append(
                    OrphanFile(
                        file=info.file,
                        datadir=scan.datadir,
                        layer=scan.layer,
                        env=scan.env,
                    )
                )
                continue
            if scan.layer != "environment" or scan.env not in indexes:
                continue
            _check_stale(
                info,
                matches,
                scan.env,
                indexes[scan.env],
                result,
                skipped_vars,
            )

    _stale_lookup_options(result)

    for env, var in sorted(skipped_vars):
        result.warnings.append(
            Warn(
                "stale_check_skipped",
                f"environment '{env}': hierarchy variable ${var} is not assigned "
                "from literal values; stale data file detection skipped for "
                "its paths",
            )
        )


def _compile_patterns(scan, config_env) -> list[_CompiledPattern]:
    compiled = []
    for entry in scan.entries:
        env = config_env.get(entry.config_file)
        for pattern, is_glob in zip(entry.patterns, entry.glob_flags):
            interps = [b.strip() for b in INTERP.findall(pattern)]
            capture = None
            if len(interps) == 1 and not is_glob:
                capture = re.compile(
                    hiera_pattern_to_regex(
                        pattern, is_glob, capture_interp=True
                    )
                )
            compiled.append(
                _CompiledPattern(
                    pattern=pattern,
                    regex=re.compile(hiera_pattern_to_regex(pattern, is_glob)),
                    capture=capture,
                    interps=interps,
                    env=env,
                )
            )
    return compiled


def _check_stale(
    info,
    matches: list[_CompiledPattern],
    env: str,
    index,
    result: AnalysisResult,
    skipped_vars,
) -> None:
    stale_reason = None
    for match in matches:
        verdict, reason = _verdict(info, match, env, index, skipped_vars)
        if verdict == "ok" or verdict == "unknown":
            return  # reachable via this pattern; not stale
        if stale_reason is None:
            stale_reason = reason
    if stale_reason is not None:
        result.stale_files.append(
            StaleFile(file=info.file, env=env, message=stale_reason)
        )


def _verdict(
    info, match: _CompiledPattern, env: str, index, skipped_vars
) -> tuple[str, str | None]:
    if not match.interps:
        return "ok", None
    if len(match.interps) != 1 or match.capture is None:
        return "unknown", None
    body = match.interps[0]
    captured = match.capture.fullmatch(info.rel)
    if captured is None:
        return "unknown", None
    stem = captured.group(1)

    if body in NODE_NAME_VARS:
        if index.node_default or not index.nodes:
            return "unknown", None
        if _matches_node(stem, index.nodes):
            return "ok", None
        return (
            "stale",
            f"no node definition in environment '{env}' matches '{stem}'",
        )

    if body == "environment":
        if stem == env:
            return "ok", None
        return (
            "stale",
            f"literally interpolated %{{environment}} is '{env}' "
            f"here, not '{stem}'",
        )

    if BARE_VAR.match(body):
        assigns = index.assignments.get(body, [])
        if not assigns or not all(a.literal for a in assigns):
            skipped_vars.add((env, body))
            return "unknown", None
        allowed = set()
        for assign in assigns:
            allowed.update(assign.values)
        if stem in allowed:
            return "ok", None
        return (
            "stale",
            f"${body} is never assigned the value '{stem}' in "
            f"environment '{env}' manifests",
        )

    return "unknown", None  # facts and other runtime-only variables


def _matches_node(name: str, nodes) -> bool:
    for node_def in nodes:
        for kind, value in node_def.patterns:
            if kind == "literal" and value == name:
                return True
            if kind == "regex":
                try:
                    if re.search(value, name):
                        return True
                except re.error:
                    return True  # unparsable regex: assume it matches
    return False


def _stale_lookup_options(result: AnalysisResult) -> None:
    all_key_names = {key.name for key in result.inventory.keys}
    seen = set()
    for index in result.indexes.values():
        for entry in index.lookup_options:
            if entry.regex:
                continue
            spot = (str(entry.file), entry.line, entry.name)
            if spot in seen:
                continue
            seen.add(spot)
            if entry.name not in all_key_names:
                result.warnings.append(
                    Warn(
                        "stale_lookup_options",
                        f"lookup_options entry '{entry.name}' names a key not found in "
                        "any scanned data",
                        file=str(entry.file),
                        line=entry.line,
                    )
                )
