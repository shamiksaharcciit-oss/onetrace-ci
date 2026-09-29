"""A deliberately restricted YAML reader for `onetrace-plan.yaml`.

WHY NOT A FULL YAML PARSER
--------------------------
A plan file carries decisions a person signed (`approved_by`), so it must mean one thing only.
Full YAML has constructs that make a file mean something its reader did not see: anchors and
aliases that copy a value from elsewhere, tags that change a type, block scalars whose
whitespace rules are easy to misread, multi-line plain scalars, and the YAML 1.1 booleans
(`yes`, `no`, `on`, `off`) that YAML 1.2 reads as strings. This reader accepts a small subset
and refuses everything else, naming the line. Anything it accepts, it reads exactly as a real
YAML parser does, with one documented difference: it never produces a number (below).

WHAT IT ACCEPTS
---------------
- block mappings, nested by indentation (spaces only), with simple keys: a letter or `_`,
  then letters, digits, spaces, `_`, `.` or `-` (never a number, a boolean or null);
- block sequences, indented under their key or at the key's own indent, whose items are
  scalars, one-line flow collections, or mappings (`- name: x` and its continuation keys);
- one-line flow sequences of scalars, `[a, "b c"]`, and one-line flow mappings of scalars,
  `{name: bm25, package: rank_bm25}`;
- plain scalars, single-quoted scalars (`''` escapes a quote) and double-quoted scalars (with
  the escapes `\\\\ \\" \\/ \\n \\t \\r \\0 \\uXXXX`);
- comments: `#` at the start of a line or after whitespace, outside quotes;
- a leading UTF-8 byte order mark, as some editors save one.

Scalars: `true`/`false` (in lower, title or upper case) are booleans; `null`, `~` and an empty
value are null; **everything else is the string as written**, including `3` and `007`. A plan
never needs a number, and a number read as an int would silently lose its spelling.

WHAT IT REFUSES
---------------
Block scalars (`|`, `>`), anchors (`&`), aliases (`*`), tags (`!`), directives (`%`), document
markers (`---`, `...`), complex keys (`?`), nested or multi-line flow collections, duplicate
keys, tab indentation, multi-line plain scalars, a plain scalar holding `: ` or a tab, and any
document whose top level is not a mapping. Also, because two YAML readers would read them
differently: an unquoted `yes`/`no`/`on`/`off` (a boolean to YAML 1.1, a string to YAML 1.2),
an unquoted date, the reserved `=` and `<<`, and anywhere in the file a line or paragraph
separator, NEL, or control character (a YAML reader breaks lines where this one would not, which
could hide a key inside a comment), or a `\\u` escape of a lone surrogate. Quote a value to
keep it as text.
"""
from __future__ import annotations

import re

__all__ = ["SubsetError", "parse"]


class SubsetError(ValueError):
    """The text is outside the supported subset, or is not well-formed. Names the line."""


#: A key starts with a letter or underscore (so it is never a number) and may hold spaces.
_KEY_RE = re.compile(r"([A-Za-z_](?:[A-Za-z0-9_.\- ]*[A-Za-z0-9_.\-])?):(?:[ ]+|$)")
#: The spellings PyYAML (YAML 1.1) reads as booleans and YAML 1.2 reads as strings.
_YAML11_BOOLS = frozenset("yes Yes YES no No NO on On ON off Off OFF".split())
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt ]|$)")
#: Line and paragraph separators and NEL are line breaks to YAML readers but not to this one's
#: line splitter, so they could hide a key inside a comment; control characters have no place in
#: a plan. All are refused wherever they appear. Tab, newline and carriage return are allowed.
_FORBIDDEN_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f  ﻿]")


def _forbidden(text: str):
    return _FORBIDDEN_RE.search(text)


def _checked_key(key: str, source: str, no: int) -> str:
    """A key a YAML reader would take as something other than a string is refused."""
    if key in _TRUE or key in _FALSE or key in _NULL or key in _YAML11_BOOLS:
        raise SubsetError(f"{source}: line {no}: the key {key!r} is a boolean or null to YAML "
                          f"readers, not a name")
    return key
_TRUE = ("true", "True", "TRUE")
_FALSE = ("false", "False", "FALSE")
_NULL = ("null", "Null", "NULL", "~", "")
_REFUSED_LEADING = {
    "|": "a block scalar",
    ">": "a folded scalar",
    "&": "an anchor",
    "*": "an alias",
    "!": "a tag",
    "%": "a directive",
    "@": "a reserved indicator",
    "`": "a reserved indicator",
    "?": "a complex key",
}
_ESCAPES = {"\\": "\\", '"': '"', "/": "/", "n": "\n", "t": "\t", "r": "\r", "0": "\0"}


class _Line:
    __slots__ = ("no", "indent", "text")

    def __init__(self, no: int, indent: int, text: str):
        self.no, self.indent, self.text = no, indent, text


def _strip_comment(body: str, source: str, no: int) -> str:
    """`body` with any trailing comment removed and right-stripped. A quote opens a quoted
    scalar only where a scalar can start (the start of the value, or after `: `, `- `, `[`,
    `{` or `,`), so the apostrophe in `it's fine` is ordinary text."""
    out = []
    i, n = 0, len(body)
    prev = ""            # the last non-space character emitted, "" at the start
    at_space = True      # the previous character was whitespace (or the start of the line)
    while i < n:
        c = body[i]
        if c == "#" and at_space:
            break
        if c in ("'", '"') and (prev in ("", "[", "{", ",") or (prev in (":", "-") and at_space)):
            j = i + 1
            while True:
                if j >= n:
                    raise SubsetError(f"{source}: line {no}: unterminated quoted scalar")
                if c == '"' and body[j] == "\\":
                    j += 2
                    continue
                if body[j] == c:
                    if c == "'" and j + 1 < n and body[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(body[i:j + 1])
            prev, at_space = c, False
            i = j + 1
            continue
        out.append(c)
        if c in (" ", "\t"):
            at_space = True
        else:
            prev, at_space = c, False
        i += 1
    return "".join(out).rstrip()


def _lines(text: str, source: str) -> list[_Line]:
    result = []
    for no, raw in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), start=1):
        stripped = raw.lstrip(" ")
        if stripped.startswith("\t") and stripped.strip():
            raise SubsetError(f"{source}: line {no}: tab indentation is refused; indent with spaces")
        indent = len(raw) - len(stripped)
        if stripped.startswith("#") or not stripped.strip():
            continue
        body = _strip_comment(stripped, source, no)
        if not body:
            continue
        if body in ("---", "...") or body.startswith(("--- ", "... ")):
            raise SubsetError(f"{source}: line {no}: a document marker is refused; one plan per file")
        if body.startswith("%"):
            raise SubsetError(f"{source}: line {no}: a directive is refused")
        result.append(_Line(no, indent, body))
    return result


def _quoted(text: str, source: str, no: int) -> str:
    """The value of a quoted scalar that is exactly `text`."""
    q = text[0]
    if len(text) < 2 or text[-1] != q:
        raise SubsetError(f"{source}: line {no}: text after a quoted scalar, or no closing quote: {text!r}")
    inner = text[1:-1]
    if q == "'":
        if re.search(r"(?<!')'(?!')", inner.replace("''", "")):
            raise SubsetError(f"{source}: line {no}: text after a quoted scalar: {text!r}")
        return inner.replace("''", "'")
    out, i = [], 0
    while i < len(inner):
        c = inner[i]
        if c == '"':
            raise SubsetError(f"{source}: line {no}: text after a quoted scalar: {text!r}")
        if c != "\\":
            out.append(c)
            i += 1
            continue
        if i + 1 >= len(inner):
            raise SubsetError(f"{source}: line {no}: a trailing backslash in {text!r}")
        e = inner[i + 1]
        if e in _ESCAPES:
            out.append(_ESCAPES[e])
            i += 2
        elif e == "u" and re.fullmatch(r"[0-9A-Fa-f]{4}", inner[i + 2:i + 6] or ""):
            code = int(inner[i + 2:i + 6], 16)
            if 0xD800 <= code <= 0xDFFF:
                raise SubsetError(f"{source}: line {no}: \\u{code:04X} is a lone surrogate, which "
                                  f"is not a character")
            if _forbidden(chr(code)):
                raise SubsetError(f"{source}: line {no}: \\u{code:04X} is a control or line-break "
                                  f"character, refused in a plan")
            out.append(chr(code))
            i += 6
        else:
            raise SubsetError(f"{source}: line {no}: the escape \\{e} is outside the subset")
    return "".join(out)


def _plain(text: str, source: str, no: int, *, flow: bool):
    first = text[0]
    if first in _REFUSED_LEADING:
        raise SubsetError(f"{source}: line {no}: {_REFUSED_LEADING[first]} ({first!r}) is refused: {text!r}")
    if first in "[]{},":
        raise SubsetError(f"{source}: line {no}: {first!r} cannot start a plain scalar here: {text!r}")
    if first == "-" and (len(text) == 1 or text[1] == " "):
        raise SubsetError(f"{source}: line {no}: a list item is not allowed here: {text!r}")
    if ": " in text or text.endswith(":"):
        raise SubsetError(f"{source}: line {no}: a plain scalar holding ': ' reads as a nested "
                          f"mapping; quote it: {text!r}")
    if " #" in text:
        raise SubsetError(f"{source}: line {no}: ' #' inside a value; quote it: {text!r}")
    if flow and any(c in text for c in "[]{},"):
        raise SubsetError(f"{source}: line {no}: a nested or unquoted flow indicator in {text!r}")
    if "\t" in text:
        raise SubsetError(f"{source}: line {no}: a tab inside a plain value; quote it: {text!r}")
    if text in ("=", "<<"):
        raise SubsetError(f"{source}: line {no}: {text!r} is reserved in YAML 1.1; quote it")
    if text in _YAML11_BOOLS:
        raise SubsetError(f"{source}: line {no}: {text!r} is a boolean to YAML 1.1 readers and a "
                          f"string to YAML 1.2 readers; quote it, or write true or false")
    if _DATE_RE.match(text):
        raise SubsetError(f"{source}: line {no}: {text!r} is a date to YAML readers; quote it")
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    if text in _NULL:
        return None
    return text


def _scalar(text: str, source: str, no: int, *, flow: bool = False):
    text = text.strip()
    if not text:
        return None
    if text[0] in ("'", '"'):
        return _quoted(text, source, no)
    return _plain(text, source, no, flow=flow)


def _split_flow(inner: str, source: str, no: int) -> list[str]:
    """Split the inside of a one-line flow collection on its top-level commas."""
    parts, cur, i = [], [], 0
    while i < len(inner):
        c = inner[i]
        if c in ("'", '"') and not "".join(cur).strip():
            j = i + 1
            while j < len(inner):
                if c == '"' and inner[j] == "\\":
                    j += 2
                    continue
                if inner[j] == c:
                    if c == "'" and j + 1 < len(inner) and inner[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            if j >= len(inner):
                raise SubsetError(f"{source}: line {no}: unterminated quoted scalar in a flow collection")
            cur.append(inner[i:j + 1])
            i = j + 1
            continue
        if c in "[]{}":
            raise SubsetError(f"{source}: line {no}: a nested flow collection is refused")
        if c == ",":
            parts.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    parts.append("".join(cur).strip())
    if parts == [""]:
        return []
    if any(not p for p in parts):
        raise SubsetError(f"{source}: line {no}: an empty entry in a flow collection")
    return parts


def _inline(text: str, source: str, no: int):
    """A value written on the same line as its key or dash."""
    if text.startswith("["):
        if not text.endswith("]"):
            raise SubsetError(f"{source}: line {no}: a flow list must open and close on one line")
        return [_scalar(p, source, no, flow=True) for p in _split_flow(text[1:-1], source, no)]
    if text.startswith("{"):
        if not text.endswith("}"):
            raise SubsetError(f"{source}: line {no}: a flow mapping must open and close on one line")
        result: dict = {}
        for part in _split_flow(text[1:-1], source, no):
            m = _KEY_RE.match(part)
            if not m:
                raise SubsetError(f"{source}: line {no}: {part!r} is not `key: value`")
            key = _checked_key(m.group(1), source, no)
            if key in result:
                raise SubsetError(f"{source}: line {no}: duplicate key {key!r}")
            result[key] = _scalar(part[m.end():], source, no, flow=True)
        return result
    return _scalar(text, source, no)


class _Reader:
    def __init__(self, lines: list[_Line], source: str):
        self.lines, self.source, self.i = lines, source, 0

    def peek(self) -> _Line | None:
        return self.lines[self.i] if self.i < len(self.lines) else None

    def fail(self, line: _Line, why: str):
        raise SubsetError(f"{self.source}: line {line.no}: {why}")

    def block(self, indent: int):
        line = self.peek()
        if line.text == "-" or line.text.startswith("- "):
            return self.sequence(line.indent)
        return self.mapping(line.indent)

    def mapping(self, indent: int, first: str | None = None, first_no: int | None = None) -> dict:
        """A block mapping at column `indent`. `first`, when given, is its first entry's text,
        already consumed (the part of a `- key: value` line after the dash)."""
        result: dict = {}
        pending = [(first, first_no)] if first is not None else []
        while True:
            if pending:
                text, no = pending.pop()
            else:
                line = self.peek()
                if line is None or line.indent < indent:
                    return result
                if line.indent > indent:
                    self.fail(line, "indented deeper than the mapping it belongs to")
                if line.text == "-" or line.text.startswith("- "):
                    if not result:
                        self.fail(line, "a list item where a mapping key was expected")
                    return result
                self.i += 1
                text, no = line.text, line.no
            m = _KEY_RE.match(text)
            if not m:
                if text.startswith("?"):
                    raise SubsetError(f"{self.source}: line {no}: a complex key ('?') is refused")
                raise SubsetError(f"{self.source}: line {no}: not `key: value` with a simple key "
                                  f"(a letter or _, then letters, digits, spaces, _ . -): {text!r}")
            key, rest = _checked_key(m.group(1), self.source, no), text[m.end():].strip()
            if key in result:
                raise SubsetError(f"{self.source}: line {no}: duplicate key {key!r}")
            if rest:
                result[key] = _inline(rest, self.source, no)
                nxt = self.peek()
                if nxt is not None and nxt.indent > indent:
                    self.fail(nxt, f"indented under {key!r}, whose value is already on its own line")
                continue
            nxt = self.peek()
            if nxt is None or nxt.indent < indent:
                result[key] = None
            elif nxt.indent == indent:
                if nxt.text == "-" or nxt.text.startswith("- "):
                    result[key] = self.sequence(indent)
                else:
                    result[key] = None
            else:
                result[key] = self.block(nxt.indent)

    def sequence(self, indent: int) -> list:
        result = []
        while True:
            line = self.peek()
            if line is None or line.indent < indent:
                return result
            if line.indent > indent:
                self.fail(line, "indented deeper than the list it belongs to")
            if not (line.text == "-" or line.text.startswith("- ")):
                if line.text.startswith("-"):
                    self.fail(line, f"a list item needs a space after the dash: {line.text!r}")
                return result
            self.i += 1
            rest = line.text[1:].lstrip(" ")
            if not rest:
                nxt = self.peek()
                if nxt is None or nxt.indent <= indent:
                    result.append(None)
                else:
                    result.append(self.block(nxt.indent))
                continue
            if _KEY_RE.match(rest):
                column = line.indent + (len(line.text) - len(rest))
                result.append(self.mapping(column, first=rest, first_no=line.no))
                continue
            result.append(_inline(rest, self.source, line.no))
            nxt = self.peek()
            if nxt is not None and nxt.indent > indent:
                self.fail(nxt, "indented under a list item whose value is already on its own line")


def parse(text: str, *, source: str) -> dict:
    """Read `text` as a mapping, or raise `SubsetError` naming the first line outside the subset."""
    if text.startswith("﻿"):
        text = text[1:]                      # a byte order mark, as an editor may save it
    bad = _forbidden(text)
    if bad:
        no = text.count("\n", 0, bad.start()) + 1
        raise SubsetError(f"{source}: line {no}: the character U+{ord(bad.group()):04X} is refused in "
                          f"a plan (a line or paragraph separator, or a control character, which "
                          f"readers can disagree about)")
    lines = _lines(text, source)
    if not lines:
        return {}
    first = lines[0]
    if first.indent != 0:
        raise SubsetError(f"{source}: line {first.no}: the first line is indented; the top level "
                          f"must be a mapping starting at column 0")
    if first.text == "-" or first.text.startswith("- "):
        raise SubsetError(f"{source}: line {first.no}: the top level must be a mapping, not a list")
    reader = _Reader(lines, source)
    result = reader.mapping(0)
    left = reader.peek()
    if left is not None:
        reader.fail(left, "not part of the top-level mapping")
    return result
