"""A tolerant tokenizer for Puppet manifests.

Not a full lexer: it exists so that strings, comments, heredocs and
regex literals become opaque single tokens, letting the extractors walk
class headers and function calls without being desynchronised by
brackets or keywords hiding inside them. It never raises on malformed
input; unknown characters become 'other' tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

MULTI_OPS = (
    "=>",
    "==",
    "=~",
    "!~",
    "!=",
    "<=",
    ">=",
    "->",
    "~>",
    "+=",
    "<<",
    ">>",
)
PUNCT = set("()[]{},=:;?|.@+-*<>!")

#: Tokens after which a '/' starts a regex literal rather than division.
REGEX_AFTER_PUNCT = {
    "(",
    "[",
    "{",
    ",",
    "=",
    "=>",
    "=~",
    "!~",
    "?",
    ":",
    ";",
    "+",
    "<<",
}
REGEX_AFTER_IDENT = {
    "node",
    "if",
    "elsif",
    "unless",
    "case",
    "and",
    "or",
    "in",
    "match",
}

IDENT_START = re.compile(r"[A-Za-z_]")
IDENT_CONT = re.compile(r"[A-Za-z0-9_]")


@dataclass
class Token:
    kind: str  # ident | var | string | number | regex | punct | op | other
    value: str
    line: int
    interpolated: bool = False  # strings/heredocs only


def tokenize(text: str) -> list[Token]:
    return _Scanner(text).scan()


class _Scanner:
    def __init__(self, text: str):
        self.text = text
        self.i = 0
        self.n = len(text)
        self.line = 1
        self.tokens: list[Token] = []
        # Heredoc bodies start on the line after the @(TAG) marker; the
        # placeholder token sits where the marker appeared.
        self.pending_heredocs: list[tuple[str, Token]] = []

    def scan(self) -> list[Token]:
        while self.i < self.n:
            ch = self.text[self.i]
            if ch == "\n":
                self.line += 1
                self.i += 1
                if self.pending_heredocs:
                    self._consume_heredocs()
            elif ch in " \t\r":
                self.i += 1
            elif ch == "#":
                self._skip_to_eol()
            elif ch == "/" and self._peek(1) == "*":
                self._skip_block_comment()
            elif ch == "'":
                self._scan_single_quoted()
            elif ch == '"':
                self._scan_double_quoted()
            elif ch == "@" and self._peek(1) == "(":
                self._scan_heredoc_marker()
            elif ch == "/":
                self._scan_regex_or_slash()
            elif ch == "$":
                self._scan_variable()
            elif ch.isdigit():
                self._scan_number()
            elif IDENT_START.match(ch) or self._at_absolute_name():
                self._scan_ident()
            else:
                self._scan_operator(ch)
        return self.tokens

    # -- helpers ---------------------------------------------------------

    def _peek(self, offset: int) -> str:
        pos = self.i + offset
        return self.text[pos] if pos < self.n else ""

    def _emit(
        self,
        kind: str,
        value: str,
        line: int | None = None,
        interpolated: bool = False,
    ) -> Token:
        token = Token(
            kind, value, line if line is not None else self.line, interpolated
        )
        self.tokens.append(token)
        return token

    def _skip_to_eol(self) -> None:
        while self.i < self.n and self.text[self.i] != "\n":
            self.i += 1

    def _skip_block_comment(self) -> None:
        end = self.text.find("*/", self.i + 2)
        if end == -1:
            self.line += self.text.count("\n", self.i)
            self.i = self.n
        else:
            self.line += self.text.count("\n", self.i, end)
            self.i = end + 2

    # -- strings ---------------------------------------------------------

    def _scan_single_quoted(self) -> None:
        start_line = self.line
        self.i += 1
        out = []
        while self.i < self.n:
            ch = self.text[self.i]
            if ch == "\\" and self._peek(1) in ("\\", "'"):
                out.append(self._peek(1))
                self.i += 2
            elif ch == "'":
                self.i += 1
                break
            else:
                if ch == "\n":
                    self.line += 1
                out.append(ch)
                self.i += 1
        self._emit("string", "".join(out), start_line, interpolated=False)

    def _scan_double_quoted(self) -> None:
        start_line = self.line
        self.i += 1
        out = []
        interpolated = False
        while self.i < self.n:
            ch = self.text[self.i]
            if ch == "\\":
                out.append(self.text[self.i : self.i + 2])
                self.i += 2
            elif ch == "$":
                interpolated = True
                if self._peek(1) == "{":
                    end = self._skip_braced_interpolation(self.i + 1)
                    out.append(self.text[self.i : end])
                    self.i = end
                else:
                    out.append(ch)
                    self.i += 1
            elif ch == '"':
                self.i += 1
                break
            else:
                if ch == "\n":
                    self.line += 1
                out.append(ch)
                self.i += 1
        self._emit(
            "string", "".join(out), start_line, interpolated=interpolated
        )

    def _skip_braced_interpolation(self, start: int) -> int:
        """From the '{' of '${', return the index just past the matching
        '}'. Quoted strings inside the interpolation are skipped, so a
        '}' or '"' inside them cannot end it early."""
        i = start
        depth = 0
        while i < self.n:
            ch = self.text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
            elif ch in ("'", '"'):
                quote = ch
                i += 1
                while i < self.n:
                    if self.text[i] == "\\":
                        i += 1
                    elif self.text[i] == quote:
                        break
                    elif self.text[i] == "\n":
                        self.line += 1
                    i += 1
            elif ch == "\n":
                self.line += 1
            i += 1
        return self.n

    # -- heredocs ----------------------------------------------------------

    HEREDOC_SPEC = re.compile(
        r"@\(\s*(?P<quote>\")?(?P<tag>[^):/\"]+)(?(quote)\")"
        r"(?::[^)/]*)?(?:/[^)]*)?\)"
    )

    def _scan_heredoc_marker(self) -> None:
        match = self.HEREDOC_SPEC.match(self.text, self.i)
        if not match:
            self._emit("punct", "@")
            self.i += 1
            return
        tag = match.group("tag").strip()
        interpolated = match.group("quote") is not None
        token = self._emit("string", "", interpolated=interpolated)
        self.pending_heredocs.append((tag, token))
        self.i = match.end()

    def _consume_heredocs(self) -> None:
        for tag, token in self.pending_heredocs:
            end_re = re.compile(
                rf"^[ \t]*(\|[ \t]*)?(-[ \t]*)?{re.escape(tag)}[ \t]*$"
            )
            body_lines = []
            while self.i < self.n:
                eol = self.text.find("\n", self.i)
                if eol == -1:
                    eol = self.n
                line_text = self.text[self.i : eol]
                self.i = min(eol + 1, self.n)
                self.line += 1
                if end_re.match(line_text):
                    break
                body_lines.append(line_text)
            token.value = "\n".join(body_lines)
        self.pending_heredocs = []

    # -- regex / division ------------------------------------------------

    def _regex_allowed(self) -> bool:
        if not self.tokens:
            return True
        prev = self.tokens[-1]
        if prev.kind in ("punct", "op"):
            return prev.value in REGEX_AFTER_PUNCT
        if prev.kind == "ident":
            return prev.value in REGEX_AFTER_IDENT
        return False

    def _scan_regex_or_slash(self) -> None:
        if self._regex_allowed():
            # Puppet regex literals cannot span lines; bail to a plain
            # slash if no closing '/' is found on this one.
            j = self.i + 1
            while j < self.n and self.text[j] != "\n":
                if self.text[j] == "\\":
                    j += 2
                    continue
                if self.text[j] == "/":
                    self._emit("regex", self.text[self.i + 1 : j])
                    self.i = j + 1
                    return
                j += 1
        self._emit("punct", "/")
        self.i += 1

    # -- names and numbers -------------------------------------------------

    def _at_absolute_name(self) -> bool:
        return (
            self.text[self.i] == ":"
            and self._peek(1) == ":"
            and bool(IDENT_START.match(self._peek(2) or " "))
        )

    def _read_qualified_name(self, start: int) -> tuple[str, int]:
        i = start
        if self.text.startswith("::", i):
            i += 2
        parts = []
        while i < self.n and IDENT_START.match(self.text[i]):
            j = i
            while j < self.n and IDENT_CONT.match(self.text[j]):
                j += 1
            parts.append(self.text[i:j])
            if (
                self.text.startswith("::", j)
                and j + 2 < self.n
                and IDENT_START.match(self.text[j + 2])
            ):
                i = j + 2
            else:
                i = j
                break
        return "::".join(parts), i

    def _scan_ident(self) -> None:
        name, end = self._read_qualified_name(self.i)
        self._emit("ident", name)
        self.i = end

    def _scan_variable(self) -> None:
        start = self.i + 1
        if start < self.n and self.text[start].isdigit():
            j = start
            while j < self.n and self.text[j].isdigit():
                j += 1
            self._emit("var", self.text[start:j])
            self.i = j
            return
        name, end = self._read_qualified_name(start)
        if not name:
            self._emit("punct", "$")
            self.i += 1
            return
        self._emit("var", name)
        self.i = end

    def _scan_number(self) -> None:
        j = self.i
        while j < self.n and (
            IDENT_CONT.match(self.text[j]) or self.text[j] == "."
        ):
            j += 1
        self._emit("number", self.text[self.i : j])
        self.i = j

    def _scan_operator(self, ch: str) -> None:
        for op in MULTI_OPS:
            if self.text.startswith(op, self.i):
                self._emit("op", op)
                self.i += len(op)
                return
        self._emit("punct" if ch in PUNCT else "other", ch)
        self.i += 1
