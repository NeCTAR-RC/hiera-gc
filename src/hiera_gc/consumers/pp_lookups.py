"""Extraction of explicit hiera lookups from a Puppet token stream.

Handles lookup()/hiera()/hiera_array()/hiera_hash()/hiera_include()
and Deferred('lookup', [...]). Works for .pp manifests and (via the
EPP extractor) template code.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from hiera_gc.consumers.model import Consumer
from hiera_gc.consumers.pp_tokens import Token

LOOKUP_FUNCS = {"lookup", "hiera", "hiera_array", "hiera_hash",
                "hiera_include"}
MERGING_FUNCS = {"hiera_array", "hiera_hash"}
INTERP_IN_STRING = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_:][\w:]*")
OPEN = {"(", "[", "{"}
CLOSE = {")", "]", "}"}


def extract_lookups(tokens: List[Token], file: Path,
                    kind: str = "pp_lookup") -> List[Consumer]:
    consumers: List[Consumer] = []
    i = 0
    n = len(tokens)
    while i < n:
        token = tokens[i]
        if token.kind == "ident" and token.value in LOOKUP_FUNCS \
                and _is_call(tokens, i):
            args = _read_args(tokens, i + 1)
            consumers.extend(_classify_call(
                token.value, args, file, token.line, kind))
            i += 2
        elif token.kind == "ident" and token.value == "Deferred" \
                and _is_call(tokens, i):
            args = _read_args(tokens, i + 1)
            consumers.extend(_classify_deferred(args, file, token.line,
                                                kind))
            i += 2
        else:
            i += 1
    return consumers


def _is_call(tokens: List[Token], i: int) -> bool:
    if i + 1 >= len(tokens):
        return False
    follower = tokens[i + 1]
    if not (follower.kind == "punct" and follower.value == "("):
        return False
    # `$x.lookup(...)` method-style or `foo::lookup` would have been one
    # ident token; a preceding '.' means a method call on something else.
    if i > 0 and tokens[i - 1].kind == "punct" and tokens[i - 1].value == ".":
        return False
    return True


def _read_args(tokens: List[Token], paren: int) -> List[List[Token]]:
    """paren sits on '('. Returns the argument token lists."""
    args: List[List[Token]] = [[]]
    depth = 0
    k = paren
    while k < len(tokens):
        token = tokens[k]
        if token.kind == "punct" and token.value in OPEN:
            depth += 1
            if depth > 1:
                args[-1].append(token)
        elif token.kind == "punct" and token.value in CLOSE:
            depth -= 1
            if depth == 0:
                break
            args[-1].append(token)
        elif token.kind == "punct" and token.value == "," and depth == 1:
            args.append([])
        elif depth >= 1:
            args[-1].append(token)
        k += 1
    if args == [[]]:
        return []
    return args


def _classify_call(func: str, args: List[List[Token]], file: Path,
                   line: int, kind: str) -> List[Consumer]:
    merge = func in MERGING_FUNCS or _has_merge(func, args)
    detail = "%s()" % func
    if not args:
        return []
    first = args[0]
    keys = _keys_from_arg(first)
    if keys is None:
        return [Consumer(kind="dynamic", key=None, pattern=None, file=file,
                         line=line, detail=detail, merge=merge)]
    consumers = []
    for value, interpolated in keys:
        if interpolated:
            pattern = INTERP_IN_STRING.sub("*", value)
            consumers.append(Consumer(
                kind=kind, key=None, pattern=pattern, file=file,
                line=line, detail=detail, merge=merge))
        else:
            consumers.append(Consumer(
                kind=kind, key=value, pattern=None, file=file,
                line=line, detail=detail, merge=merge))
    return consumers


def _keys_from_arg(arg: List[Token]) -> Optional[list]:
    """Returns [(value, interpolated)] or None when opaque/dynamic."""
    if not arg:
        return None
    head = arg[0]
    if head.kind == "string" and len(arg) == 1:
        return [(head.value, head.interpolated)]
    if head.kind == "punct" and head.value == "[":
        keys = []
        for token in arg[1:]:
            if token.kind == "string":
                keys.append((token.value, token.interpolated))
            elif token.kind == "punct" and token.value in (",", "]"):
                continue
            else:
                return None
        return keys or None
    if head.kind == "punct" and head.value == "{":
        # Hash form: lookup({'name' => 'key', ...})
        for idx, token in enumerate(arg):
            if token.kind in ("string", "ident") and token.value == "name" \
                    and idx + 2 < len(arg) \
                    and arg[idx + 1].kind == "op" \
                    and arg[idx + 1].value == "=>" \
                    and arg[idx + 2].kind == "string":
                key = arg[idx + 2]
                return [(key.value, key.interpolated)]
        return None
    return None


def _has_merge(func: str, args: List[List[Token]]) -> bool:
    if func == "lookup":
        if len(args) >= 3:
            return True
        for arg in args:
            for token in arg:
                if token.kind == "string" and token.value == "merge":
                    return True
    return False


def _classify_deferred(args: List[List[Token]], file: Path, line: int,
                       kind: str) -> List[Consumer]:
    if len(args) < 2:
        return []
    head = args[0]
    if not (len(head) == 1 and head[0].kind == "string"
            and head[0].value in LOOKUP_FUNCS):
        return []
    func = head[0].value
    return _classify_call(func, [_strip_brackets(args[1])], file, line, kind)


def _strip_brackets(arg: List[Token]) -> List[Token]:
    # Deferred('lookup', ['key']) wraps the lookup args in an array; the
    # first element is the key.
    if arg and arg[0].kind == "punct" and arg[0].value == "[":
        inner = [t for t in arg[1:]
                 if not (t.kind == "punct" and t.value in ("]",))]
        head = []
        for token in inner:
            if token.kind == "punct" and token.value == ",":
                break
            head.append(token)
        return head
    return arg
