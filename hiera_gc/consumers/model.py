from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Consumer:
    """One piece of evidence that a hiera key is (or may be) read.

    Exactly one of key/pattern is set, except for kind='dynamic'
    (an opaque lookup whose key is built at runtime) where both are
    None and the consumer only feeds a warning.
    """
    kind: str  # apl | pp_lookup | epp_lookup | erb_lookup | ruby_lookup |
               # erb_var | ruby_var | data_lookup | data_alias |
               # data_var_interp | lookup_options_ref | mention | dynamic
    key: Optional[str]
    pattern: Optional[str]  # fnmatch-style, from interpolated lookup keys
    file: Path
    line: int
    detail: str = ""
    merge: bool = False  # the call merges across hierarchy levels

    def location(self) -> str:
        return "%s:%d" % (self.file, self.line)


#: Consumer kinds that prove a key is read (USED).
STRONG_KINDS = frozenset({
    "apl", "pp_lookup", "epp_lookup", "erb_lookup", "ruby_lookup",
    "data_lookup", "data_alias",
})

#: Consumer kinds that only suggest a key may be read (POSSIBLY_USED).
WEAK_KINDS = frozenset({
    "erb_var", "ruby_var", "data_var_interp", "lookup_options_ref",
    "mention",
})
