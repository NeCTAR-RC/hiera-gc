"""Extraction of lookups from EPP templates.

Non-code regions are blanked out (newlines preserved) so the Puppet
tokenizer can run over the result with correct line numbers.
"""

from __future__ import annotations

from pathlib import Path
import re

from hiera_gc.consumers.model import Consumer
from hiera_gc.consumers.pp_lookups import extract_lookups
from hiera_gc.consumers.pp_tokens import tokenize

_NON_NEWLINE = re.compile(r"[^\n]")


def _blank(text: str) -> str:
    return _NON_NEWLINE.sub(" ", text)


def mask_epp(text: str) -> str:
    """Blank everything except the code inside <% ... %> tags."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        start = text.find("<%", i)
        if start == -1:
            out.append(_blank(text[i:]))
            break
        out.append(_blank(text[i:start]))
        if text.startswith("<%#", start):  # comment tag
            end = text.find("%>", start)
            end = n if end == -1 else end + 2
            out.append(_blank(text[start:end]))
            i = end
            continue
        code_start = start + 2
        while code_start < n and text[code_start] in "=-":
            code_start += 1
        out.append(" " * (code_start - start))
        end = text.find("%>", code_start)
        if end == -1:
            out.append(text[code_start:])
            break
        code_end = end - 1 if text[end - 1] == "-" else end
        out.append(text[code_start:code_end])
        out.append(_blank(text[code_end : end + 2]))
        i = end + 2
    return "".join(out)


def extract_epp(text: str, file: Path) -> list[Consumer]:
    tokens = tokenize(mask_epp(text))
    return extract_lookups(tokens, file, kind="epp_lookup")
