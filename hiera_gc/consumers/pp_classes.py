"""Extraction of class/define headers, node definitions and top-scope
variable assignments from a Puppet token stream."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from hiera_gc.consumers.pp_tokens import Token

CLASS_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*(::[a-z_][a-z0-9_]*)*$")
LITERAL_VALUE_IDENTS = {"true", "false", "undef", "default"}
#: Idents that mean "a new statement started" while looking ahead for a
#: selector expression.
STATEMENT_IDENTS = {"class", "define", "node", "include", "require",
                    "contain", "if", "case", "unless"}
OPEN_BRACKETS = {"(", "[", "{"}
CLOSE_BRACKETS = {")", "]", "}"}


@dataclass
class ParamDef:
    name: str
    line: int


@dataclass
class ClassDef:
    name: str
    params: List[ParamDef]
    line: int
    kind: str  # class | define


@dataclass
class NodeDef:
    patterns: List[Tuple[str, str]]  # (literal|regex|default, value)
    line: int


@dataclass
class VarAssign:
    var: str
    values: List[str]
    literal: bool  # True when `values` is the complete possible set
    line: int


@dataclass
class PPDefinitions:
    classes: List[ClassDef] = field(default_factory=list)
    defines: List[ClassDef] = field(default_factory=list)
    nodes: List[NodeDef] = field(default_factory=list)
    assignments: List[VarAssign] = field(default_factory=list)


def extract_definitions(tokens: List[Token]) -> PPDefinitions:
    result = PPDefinitions()
    i = 0
    n = len(tokens)
    while i < n:
        token = tokens[i]
        if token.kind == "ident" and token.value in ("class", "define"):
            i = _parse_definition(tokens, i, result)
        elif token.kind == "ident" and token.value == "node":
            i = _parse_node(tokens, i, result)
        elif (token.kind == "var" and i + 1 < n
              and tokens[i + 1].kind == "punct"
              and tokens[i + 1].value == "="):
            i = _parse_assignment(tokens, i, result)
        else:
            i += 1
    return result


def _parse_definition(tokens: List[Token], i: int,
                      result: PPDefinitions) -> int:
    kind = tokens[i].value
    j = i + 1
    if j >= len(tokens) or tokens[j].kind != "ident":
        return i + 1  # `class { 'x': }` resource form, `class =>`, etc.
    name_token = tokens[j]
    if not CLASS_NAME_RE.match(name_token.value):
        return i + 1
    j += 1

    params: List[ParamDef] = []
    # Grammar puts `inherits` after the parameter list, but tolerate
    # either order.
    for _ in range(2):
        j = _skip_inherits(tokens, j)
        if j < len(tokens) and tokens[j].kind == "punct" \
                and tokens[j].value == "(":
            params, j = _parse_params(tokens, j)

    definition = ClassDef(name=name_token.value, params=params,
                          line=name_token.line, kind=kind)
    (result.classes if kind == "class" else result.defines).append(definition)
    return j


def _skip_inherits(tokens: List[Token], j: int) -> int:
    if j + 1 < len(tokens) and tokens[j].kind == "ident" \
            and tokens[j].value == "inherits" \
            and tokens[j + 1].kind == "ident":
        return j + 2
    return j


def _parse_params(tokens: List[Token], j: int) -> Tuple[List[ParamDef], int]:
    """j sits on '('. Returns the params and the index past ')'."""
    depth = 0
    chunks: List[List[Token]] = [[]]
    k = j
    while k < len(tokens):
        token = tokens[k]
        if token.kind == "punct" and token.value in OPEN_BRACKETS:
            depth += 1
            if depth > 1:
                chunks[-1].append(token)
        elif token.kind == "punct" and token.value in CLOSE_BRACKETS:
            depth -= 1
            if depth == 0:
                k += 1
                break
            chunks[-1].append(token)
        elif token.kind == "punct" and token.value == "," and depth == 1:
            chunks.append([])
        else:
            chunks[-1].append(token)
        k += 1

    params = []
    for chunk in chunks:
        param = _param_from_chunk(chunk)
        if param is not None:
            params.append(param)
    return params, k


def _param_from_chunk(chunk: List[Token]) -> Optional[ParamDef]:
    depth = 0
    for token in chunk:
        if token.kind == "punct" and token.value in OPEN_BRACKETS:
            depth += 1
        elif token.kind == "punct" and token.value in CLOSE_BRACKETS:
            depth -= 1
        elif depth == 0:
            if token.kind == "punct" and token.value == "=":
                return None  # no name before the default: malformed
            if token.kind == "var":
                return ParamDef(name=token.value, line=token.line)
    return None


def _parse_node(tokens: List[Token], i: int, result: PPDefinitions) -> int:
    patterns: List[Tuple[str, str]] = []
    j = i + 1
    while j < len(tokens):
        token = tokens[j]
        if token.kind == "string":
            patterns.append(("literal", token.value))
        elif token.kind == "regex":
            patterns.append(("regex", token.value))
        elif token.kind == "ident" and token.value == "default":
            patterns.append(("default", "default"))
        elif token.kind == "punct" and token.value == ",":
            pass
        elif token.kind == "punct" and token.value == "{":
            break
        else:
            break
        j += 1
    if patterns:
        result.nodes.append(NodeDef(patterns=patterns, line=tokens[i].line))
        return j
    return i + 1


def _parse_assignment(tokens: List[Token], i: int,
                      result: PPDefinitions) -> int:
    var = tokens[i]
    j = i + 2
    if j >= len(tokens):
        return j

    selector_brace = _find_selector(tokens, j)
    if selector_brace is not None:
        values, literal = _parse_selector_values(tokens, selector_brace)
        result.assignments.append(VarAssign(
            var=var.value, values=values, literal=literal, line=var.line))
        return i + 2

    value = tokens[j]
    continues = (j + 1 < len(tokens)
                 and (tokens[j + 1].kind == "op"
                      or (tokens[j + 1].kind == "punct"
                          and tokens[j + 1].value in "+-*/.?[")))
    if value.kind == "string" and not value.interpolated and not continues:
        assign = VarAssign(var=var.value, values=[value.value],
                           literal=True, line=var.line)
    elif value.kind == "number" and not continues:
        assign = VarAssign(var=var.value, values=[value.value],
                           literal=True, line=var.line)
    else:
        assign = VarAssign(var=var.value, values=[], literal=False,
                           line=var.line)
    result.assignments.append(assign)
    return i + 2


def _find_selector(tokens: List[Token], j: int) -> Optional[int]:
    """Look ahead from the token after '=' for `? {` introducing a
    selector. Returns the index of the '{', or None."""
    depth = 0
    for k in range(j, min(j + 60, len(tokens))):
        token = tokens[k]
        if token.kind == "punct" and token.value in OPEN_BRACKETS:
            if (token.value == "{" and depth == 0 and k > j
                    and tokens[k - 1].kind == "punct"
                    and tokens[k - 1].value == "?"):
                return k
            depth += 1
        elif token.kind == "punct" and token.value in CLOSE_BRACKETS:
            depth -= 1
            if depth < 0:
                return None
        elif depth == 0:
            if token.kind == "ident" and token.value in STATEMENT_IDENTS:
                return None
            if (token.kind == "var" and k + 1 < len(tokens)
                    and tokens[k + 1].kind == "punct"
                    and tokens[k + 1].value == "=" and k > j):
                return None  # next assignment started
    return None


def _parse_selector_values(tokens: List[Token],
                           brace: int) -> Tuple[List[str], bool]:
    """Collect the value side of every `match => value` pair inside the
    selector braces. literal=False means at least one value was an
    expression, so the collected set is incomplete."""
    values: List[str] = []
    literal = True
    depth = 1
    k = brace + 1
    while k < len(tokens) and depth > 0:
        token = tokens[k]
        if token.kind == "punct" and token.value in OPEN_BRACKETS:
            depth += 1
        elif token.kind == "punct" and token.value in CLOSE_BRACKETS:
            depth -= 1
        elif token.kind == "op" and token.value == "=>" and depth == 1:
            if not _read_selector_value(tokens, k + 1, values):
                literal = False
        k += 1
    return values, literal


def _read_selector_value(tokens: List[Token], k: int,
                         values: List[str]) -> bool:
    """A value is only collectible when it is a single literal token
    immediately followed by ',' or '}'."""
    if k >= len(tokens):
        return False
    token = tokens[k]
    follower = tokens[k + 1] if k + 1 < len(tokens) else None
    if not (follower is not None and follower.kind == "punct"
            and follower.value in (",", "}")):
        return False
    if token.kind == "string" and not token.interpolated:
        values.append(token.value)
    elif token.kind == "number":
        values.append(token.value)
    elif token.kind == "ident" and token.value in LITERAL_VALUE_IDENTS:
        values.append(token.value)
    else:
        return False
    return True
