"""Consumers found inside hiera data itself: %{lookup(...)},
%{alias(...)}, %{hiera(...)}, qualified variable interpolations, and
lookup_options entries."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

from hiera_gc.analysis import Warn
from hiera_gc.consumers.model import Consumer
from hiera_gc.yamlloc import LoadedDoc, iter_string_scalars

INTERP_TOKEN = re.compile(r"%\{([^}]*)\}")
FUNCTION_FORM = re.compile(
    r"^(lookup|hiera|alias|literal|scope)\(\s*['\"]?([^'\")]*)['\"]?\s*\)$")
#: Variable namespaces that are facts/server state, not hiera keys.
FACT_PREFIXES = ("facts.", "trusted.", "server_facts.", "::facts.")

LOOKUP_OPTIONS_KEY = "lookup_options"


@dataclass(frozen=True)
class LookupOptionsEntry:
    name: str  # key name, or regex source when regex=True
    regex: bool
    merge: Optional[str]  # merge strategy name, if configured
    file: Path
    line: int


def extract_data_consumers(
        doc: LoadedDoc) -> Tuple[List[Consumer], List[LookupOptionsEntry],
                                 List[Warn]]:
    consumers: List[Consumer] = []
    entries: List[LookupOptionsEntry] = []
    warnings: List[Warn] = []

    for top in doc.keys:
        if top.name == LOOKUP_OPTIONS_KEY:
            entries.extend(_parse_lookup_options(top, doc.file, warnings))
            continue
        for value, line in _strings_of(top):
            if value.lstrip().startswith("ENC["):
                continue  # ciphertext is opaque, never interpolated
            _scan_string(value, line, doc.file, consumers, warnings)
    return consumers, entries, warnings


def _strings_of(top):
    if top.node is not None:
        return iter_string_scalars(top.node)
    return _iter_json_strings(top.json_value)


def _iter_json_strings(value):
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            yield current, 0
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, dict):
            stack.extend(current.keys())
            stack.extend(current.values())


def _scan_string(value: str, line: int, file: Path,
                 consumers: List[Consumer], warnings: List[Warn]) -> None:
    for match in INTERP_TOKEN.finditer(value):
        body = match.group(1).strip()
        function = FUNCTION_FORM.match(body)
        if function:
            name, arg = function.group(1), function.group(2).strip()
            if name == "literal":
                continue
            if name in ("lookup", "hiera"):
                consumers.append(Consumer(
                    kind="data_lookup", key=arg, pattern=None, file=file,
                    line=line, detail="%%{%s(...)}" % name))
            elif name == "alias":
                if value.strip() != match.group(0):
                    warnings.append(Warn(
                        "data_file",
                        "alias('%s') embedded in a longer string; hiera "
                        "only supports alias() as the entire value" % arg,
                        file=str(file), line=line))
                consumers.append(Consumer(
                    kind="data_alias", key=arg, pattern=None, file=file,
                    line=line, detail="%{alias(...)}"))
            elif name == "scope":
                _add_var_consumer(arg, line, file, consumers)
            continue
        _add_var_consumer(body, line, file, consumers)


def _add_var_consumer(var: str, line: int, file: Path,
                      consumers: List[Consumer]) -> None:
    var = var.lstrip(":") if var.startswith("::") else var
    if not var or var.startswith(FACT_PREFIXES) or "::" not in var:
        # Bare facts and top-scope variables (e.g. %{nodegroup}) are not
        # hiera keys.
        return
    consumers.append(Consumer(
        kind="data_var_interp", key=var, pattern=None, file=file,
        line=line, detail="%{var} interpolation"))


def _parse_lookup_options(top, file: Path,
                          warnings: List[Warn]) -> List[LookupOptionsEntry]:
    entries: List[LookupOptionsEntry] = []
    if top.node is None:
        if isinstance(top.json_value, dict):
            for name, options in top.json_value.items():
                entries.append(LookupOptionsEntry(
                    name=str(name).lstrip("^"),
                    regex=str(name).startswith("^"),
                    merge=_merge_of_value(options), file=file, line=0))
        return entries
    if not isinstance(top.node, yaml.MappingNode):
        warnings.append(Warn(
            "data_file", "lookup_options is not a hash", file=str(file),
            line=top.line))
        return entries
    for key_node, value_node in top.node.value:
        if not isinstance(key_node, yaml.ScalarNode):
            continue
        raw = str(key_node.value)
        entries.append(LookupOptionsEntry(
            name=raw.lstrip("^") if raw.startswith("^") else raw,
            regex=raw.startswith("^"),
            merge=_merge_of_node(value_node),
            file=file, line=key_node.start_mark.line + 1))
    return entries


def _merge_of_node(node) -> Optional[str]:
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) \
                and str(key_node.value) == "merge":
            if isinstance(value_node, yaml.ScalarNode):
                return str(value_node.value)
            if isinstance(value_node, yaml.MappingNode):
                for sub_key, sub_value in value_node.value:
                    if isinstance(sub_key, yaml.ScalarNode) \
                            and str(sub_key.value) == "strategy" \
                            and isinstance(sub_value, yaml.ScalarNode):
                        return str(sub_value.value)
                return "hash"  # merge options present, strategy unstated
    return None


def _merge_of_value(options) -> Optional[str]:
    if not isinstance(options, dict):
        return None
    merge = options.get("merge")
    if isinstance(merge, str):
        return merge
    if isinstance(merge, dict):
        return str(merge.get("strategy", "hash"))
    return None
