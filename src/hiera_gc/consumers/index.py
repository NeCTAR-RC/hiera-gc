"""Builds the per-environment consumer index by driving every extractor
over the environment's visible files."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

from hiera_gc.consumers.data_interp import LookupOptionsEntry
from hiera_gc.consumers.model import Consumer
from hiera_gc.consumers.pp_classes import ClassDef, NodeDef, VarAssign
from hiera_gc.filecache import ExtractorCache, MENTION_CAP, MentionMap
from hiera_gc.scope import EnvScope
from hiera_gc.yamlloc import LoadedDoc


@dataclass
class ConsumerIndex:
    env: str
    exact: Dict[str, List[Consumer]] = field(default_factory=dict)
    #: lookup('a.b.c') digs under top-level key 'a'.
    dig: Dict[str, List[Consumer]] = field(default_factory=dict)
    #: ...but might also target a literal key named 'a.b.c'.
    dotted_full: Dict[str, List[Consumer]] = field(default_factory=dict)
    patterns: List[Consumer] = field(default_factory=list)
    dynamics: List[Consumer] = field(default_factory=list)
    lookup_options: List[LookupOptionsEntry] = field(default_factory=list)
    classes: Dict[str, Tuple[ClassDef, Path]] = field(default_factory=dict)
    defines: Dict[str, Tuple[ClassDef, Path]] = field(default_factory=dict)
    nodes: List[NodeDef] = field(default_factory=list)
    node_default: bool = False
    assignments: Dict[str, List[VarAssign]] = field(default_factory=dict)
    mentions: MentionMap = field(default_factory=dict)
    hiera_includes: List[Consumer] = field(default_factory=list)


def build_consumer_index(
        scope: EnvScope, cache: ExtractorCache,
        data_docs: List[Tuple[Path, LoadedDoc]]) -> ConsumerIndex:
    index = ConsumerIndex(env=scope.env.name)

    for path in scope.pp_files:
        result = cache.pp(path)
        if result is None:
            continue
        for class_def in result.defs.classes:
            index.classes.setdefault(class_def.name, (class_def, path))
        for define_def in result.defs.defines:
            index.defines.setdefault(define_def.name, (define_def, path))
        for node_def in result.defs.nodes:
            index.nodes.append(node_def)
            if any(kind == "default" for kind, _ in node_def.patterns):
                index.node_default = True
        for assign in result.defs.assignments:
            index.assignments.setdefault(assign.var, []).append(assign)
        _add_consumers(index, result.consumers)
        _merge_mentions(index.mentions, result.mentions)

    for path in scope.epp_files + scope.erb_files + scope.ruby_files:
        result = cache.template_or_ruby(path)
        if result is None:
            continue
        _add_consumers(index, result.consumers)
        _merge_mentions(index.mentions, result.mentions)

    for path, doc in data_docs:
        consumers, entries, _ = cache.data_consumers(path, doc)
        _add_consumers(index, consumers)
        index.lookup_options.extend(entries)
        _merge_mentions(index.mentions, cache.data_mentions(path))

    return index


def _add_consumers(index: ConsumerIndex,
                   consumers: List[Consumer]) -> None:
    for consumer in consumers:
        if consumer.kind == "dynamic":
            index.dynamics.append(consumer)
            continue
        if consumer.detail == "hiera_include()":
            index.hiera_includes.append(consumer)
        if consumer.pattern is not None:
            index.patterns.append(consumer)
            continue
        key = consumer.key or ""
        if '"' in key:
            # lookup('"a.b.c"') targets a literal dotted key.
            index.exact.setdefault(key.replace('"', ""), []).append(consumer)
        elif "." in key:
            first = key.split(".", 1)[0]
            index.dig.setdefault(first, []).append(consumer)
            index.dotted_full.setdefault(key, []).append(consumer)
        else:
            index.exact.setdefault(key, []).append(consumer)


def _merge_mentions(target: MentionMap, source: MentionMap) -> None:
    for token, locations in source.items():
        slots = target.setdefault(token, [])
        for location in locations:
            if len(slots) >= MENTION_CAP:
                break
            slots.append(location)
