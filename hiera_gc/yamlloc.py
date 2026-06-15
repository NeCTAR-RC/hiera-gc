"""Line-aware YAML loading via the compose API.

Hiera data files are never constructed into Python objects here; we walk
the composed node graph so that every top-level key keeps its source
line, duplicate keys survive (the constructor would silently drop them),
and values can be digested without ever leaving this module.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Set, Tuple

import yaml

MERGE_TAG = "tag:yaml.org,2002:merge"
STR_TAG = "tag:yaml.org,2002:str"


class DataFileError(Exception):
    def __init__(self, message: str, line: int = 0):
        super().__init__(message)
        self.message = message
        self.line = line


@dataclass
class TopKey:
    name: str
    line: int  # 1-based; 0 means unknown (json fallback)
    node: Optional[yaml.Node]  # value node; None when loaded via json
    json_value: object = None
    from_merge: bool = False

    def digest(self) -> str:
        if self.node is not None:
            return node_digest(self.node)
        canonical = json.dumps(self.json_value, sort_keys=True,
                               separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class LoadedDoc:
    file: Path
    keys: List[TopKey] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    #: Names defined more than once at the top level (loader keeps the
    #: last occurrence; the fixer must not touch these).
    duplicates: Set[str] = field(default_factory=set)
    #: Root mapping uses flow style ({...}, e.g. JSON-as-YAML); line
    #: spans of individual keys are not removable.
    flow_root: bool = False


def load_data_file(path: Path) -> LoadedDoc:
    """Load a hiera data file (.yaml/.yml/.eyaml/.json).

    Raises DataFileError when the file cannot be parsed at all.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        roots = [r for r in yaml.compose_all(text, Loader=yaml.SafeLoader)
                 if r is not None]
    except yaml.YAMLError as exc:
        if path.suffix == ".json":
            return _load_json(path, text)
        mark = getattr(exc, "problem_mark", None)
        raise DataFileError(
            "YAML parse error: %s" % str(exc).replace("\n", " "),
            line=(mark.line + 1) if mark else 0)

    doc = LoadedDoc(file=path)
    roots = [r for r in roots
             if not (isinstance(r, yaml.ScalarNode)
                     and r.tag == "tag:yaml.org,2002:null")]
    if not roots:
        doc.problems.append("empty data file")
        return doc
    if len(roots) > 1:
        doc.problems.append(
            "multi-document file (%d documents); hiera reads only the first"
            % len(roots))
    root = roots[0]
    if not isinstance(root, yaml.MappingNode):
        doc.problems.append("root is not a mapping (hiera expects a hash)")
        return doc
    doc.flow_root = bool(root.flow_style)

    explicit: List[Tuple[str, yaml.Node, int]] = []
    merge_targets: List[yaml.MappingNode] = []
    seen_lines = {}
    for key_node, value_node in root.value:
        if key_node.tag == MERGE_TAG:
            merge_targets.extend(_merge_mappings(value_node))
            continue
        if not isinstance(key_node, yaml.ScalarNode):
            doc.problems.append(
                "non-scalar top-level key at line %d"
                % (key_node.start_mark.line + 1))
            continue
        name = str(key_node.value)
        line = key_node.start_mark.line + 1
        if name in seen_lines:
            doc.problems.append(
                "duplicate top-level key '%s' (lines %d and %d)"
                % (name, seen_lines[name], line))
            doc.duplicates.add(name)
            # YAML loaders keep the last occurrence; mirror that.
            explicit = [e for e in explicit if e[0] != name]
        seen_lines[name] = line
        explicit.append((name, value_node, line))

    names = {name for name, _, _ in explicit}
    for name, value_node, line in explicit:
        doc.keys.append(TopKey(name=name, line=line, node=value_node))

    # YAML merge semantics: explicit keys win; among multiple merge
    # targets, the first defining a key wins.
    for target in merge_targets:
        for key_node, value_node in target.value:
            if not isinstance(key_node, yaml.ScalarNode):
                continue
            name = str(key_node.value)
            if name in names:
                continue
            names.add(name)
            doc.keys.append(TopKey(
                name=name, line=key_node.start_mark.line + 1,
                node=value_node, from_merge=True))
    return doc


def _merge_mappings(node: yaml.Node) -> List[yaml.MappingNode]:
    if isinstance(node, yaml.MappingNode):
        return [node]
    if isinstance(node, yaml.SequenceNode):
        return [n for n in node.value if isinstance(n, yaml.MappingNode)]
    return []


def _load_json(path: Path, text: str) -> LoadedDoc:
    doc = LoadedDoc(file=path)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise DataFileError("JSON parse error: %s" % exc)
    if not isinstance(data, dict):
        doc.problems.append("root is not an object (hiera expects a hash)")
        return doc
    doc.problems.append("parsed via json fallback; line numbers unknown")
    for name, value in data.items():
        doc.keys.append(TopKey(name=str(name), line=0, node=None,
                               json_value=value))
    return doc


def iter_string_scalars(node: yaml.Node) -> Iterator[Tuple[str, int]]:
    """Yield (value, line) for every string scalar under node.

    Alias-safe: shared nodes are visited once, cycles cannot recurse.
    """
    seen = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, yaml.ScalarNode):
            if current.tag == STR_TAG:
                yield str(current.value), current.start_mark.line + 1
        elif isinstance(current, yaml.SequenceNode):
            stack.extend(current.value)
        elif isinstance(current, yaml.MappingNode):
            for key_node, value_node in current.value:
                stack.append(key_node)
                stack.append(value_node)


def node_digest(node: yaml.Node) -> str:
    """SHA-256 of a canonical, order-insensitive serialisation of node.

    Used to compare hiera values for equality without ever keeping or
    exposing the values themselves.
    """
    return hashlib.sha256(
        _canonical(node, {}, set()).encode("utf-8", "replace")).hexdigest()


def _canonical(node: yaml.Node, memo: dict, in_progress: set) -> str:
    if id(node) in in_progress:
        return "CYCLE"
    if id(node) in memo:
        return memo[id(node)]
    in_progress.add(id(node))
    if isinstance(node, yaml.ScalarNode):
        result = "S(%s|%d:%s)" % (node.tag, len(str(node.value)), node.value)
    elif isinstance(node, yaml.SequenceNode):
        result = "L[%s]" % ",".join(
            _canonical(item, memo, in_progress) for item in node.value)
    elif isinstance(node, yaml.MappingNode):
        pairs = sorted(
            "%s=>%s" % (_canonical(k, memo, in_progress),
                        _canonical(v, memo, in_progress))
            for k, v in node.value)
        result = "M{%s}" % ",".join(pairs)
    else:
        result = "?(%r)" % node
    in_progress.discard(id(node))
    memo[id(node)] = result
    return result
