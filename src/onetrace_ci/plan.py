"""`onetrace-plan.yaml`: the plan-file subset the gate reads.

WHY A HAND-ROLLED PARSER, NOT PyYAML
-------------------------------------
This package depends on `onetrace`, pinned by hash, and nothing else outside
the standard library -- a real YAML parser is a real dependency that scope
does not allow. What the five known keys actually need is a narrow,
deliberately restricted subset: flat `key: value` scalars, and `key:`
followed by an indented `- item` list of scalars. This module parses
exactly that subset and refuses anything it cannot represent unambiguously
-- a nested mapping, a multi-line block scalar, an anchor or alias, a
flow-style `[a, b]` list -- rather than silently guessing at a real YAML
document's full grammar. A plan file that stays inside this subset (which
every key below does) parses identically here and under a real YAML
parser; one that doesn't is refused, loudly, naming the line, never
misread as something else.

THE FIVE KEYS, AND NOTHING ELSE READ SILENTLY
-----------------------------------------------
`format`, `approved_boundaries`, `known_limits`, `reproduce`, `approved_by`.
Any other top-level key is ignored for gating purposes, but named in the
summary a gate run writes. A typo in a key this schema does not know about
must be visible, not silently dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_KNOWN_KEYS = frozenset({"format", "approved_boundaries", "known_limits",
                        "reproduce", "approved_by"})

_LIST_ITEM_RE = re.compile(r"^(\s+)-\s*(.*)$")
_KEY_VALUE_RE = re.compile(r"^(\S[^:]*):\s*(.*)$")
#: Indicators this restricted reader refuses outright rather than
#: misreading as a plain string: block scalars (`|`, `>`), anchors/aliases
#: (`&`, `*`), tags (`!`), flow-style collections (`[`, `{`).
_UNSUPPORTED_LEADING = ("|", ">", "&", "*", "!", "[", "{")
#: A bare (unquoted) `key: value` shape appearing where a plain scalar is
#: expected -- a nested mapping, which this subset does not represent.
_LOOKS_LIKE_NESTED_MAPPING_RE = re.compile(r"^[^'\"\s][^:]*:\s")


class PlanError(RuntimeError):
    """The plan file could not be parsed at all -- outside the supported
    subset, or malformed. Refused, never guessed at.
    """


@dataclass(frozen=True, slots=True)
class Plan:
    format: str | None
    approved_boundaries: tuple[str, ...]
    known_limits: tuple[str, ...]
    reproduce: bool
    approved_by: str
    ignored_keys: tuple[str, ...] = field(default_factory=tuple)


def _is_quoted(text: str) -> bool:
    return len(text) >= 2 and text[0] in ("'", '"') and text.endswith(text[0])


def _check_supported_scalar(text: str, *, context: str, source: str, lineno: int) -> None:
    """Raises `PlanError`, naming the line, for anything this restricted
    reader cannot represent unambiguously as a plain scalar. Called on
    every scalar this module reads -- a top-level value and a list item
    alike -- so neither path can silently misread a construct the other
    already refuses.
    """
    stripped = text.strip()
    if _is_quoted(stripped):
        return
    if stripped.startswith(_UNSUPPORTED_LEADING):
        raise PlanError(
            f"{source}: line {lineno} ({context}) starts with {stripped[0]!r}, "
            f"outside the supported subset (block scalars, anchors/aliases, "
            f"tags and flow-style collections are all refused, not guessed at): "
            f"{text!r}")
    if _LOOKS_LIKE_NESTED_MAPPING_RE.match(stripped):
        raise PlanError(
            f"{source}: line {lineno} ({context}) looks like a nested "
            f"`key: value` mapping, which this restricted reader does not "
            f"support: {text!r}")


def _scalar(text: str) -> str | bool | None:
    text = text.strip()
    if _is_quoted(text):
        return text[1:-1]
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if text in ("", "null", "~", "None"):
        return None
    return text


def _strip_comment(line: str) -> str:
    #: A `#` is a comment only outside quotes -- good enough for this
    #: restricted subset, where no scalar this schema uses legitimately
    #: contains one.
    if "'" in line or '"' in line:
        return line
    return line.split("#", 1)[0]


def parse_plan_text(text: str, *, source: str) -> Plan:
    lines = [_strip_comment(raw).rstrip() for raw in text.splitlines()]
    values: dict[str, object] = {}
    ignored: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line[0].isspace():
            raise PlanError(
                f"{source}: line {i + 1} is indented with no key above it -- "
                f"outside the supported flat key/list-of-scalars subset")
        m = _KEY_VALUE_RE.match(line)
        if not m:
            raise PlanError(f"{source}: line {i + 1} is not `key: value` or "
                            f"`key:` -- outside the supported subset: {line!r}")
        key, rest = m.group(1).strip(), m.group(2)
        lineno = i + 1
        i += 1
        if rest.strip():
            _check_supported_scalar(rest, context=f"value of {key!r}",
                                    source=source, lineno=lineno)
            values[key] = _scalar(rest)
            continue
        #: `key:` with nothing after it -- either an indented list follows, or
        #: the value is genuinely empty/null.
        items: list[str] = []
        while i < n and _LIST_ITEM_RE.match(lines[i]):
            item_m = _LIST_ITEM_RE.match(lines[i])
            item_text = item_m.group(2)
            _check_supported_scalar(item_text, context=f"a `- item` under {key!r}",
                                    source=source, lineno=i + 1)
            items.append(str(_scalar(item_text)))
            i += 1
        values[key] = items if items else None
        if i < n and lines[i] and lines[i][0].isspace() and not items:
            raise PlanError(
                f"{source}: line {i + 1} is indented under {key!r} but is not "
                f"a `- item` list entry -- outside the supported subset")

    for key in values:
        if key not in _KNOWN_KEYS:
            ignored.append(key)

    def as_list(key: str) -> tuple[str, ...]:
        v = values.get(key)
        if v is None:
            return ()
        if not isinstance(v, list):
            raise PlanError(f"{source}: {key!r} must be a list of scalars, got {v!r}")
        return tuple(v)

    approved_by = values.get("approved_by")
    if approved_by is None:
        approved_by = ""
    if not isinstance(approved_by, str):
        raise PlanError(f"{source}: 'approved_by' must be a string, got {approved_by!r}")

    reproduce_val = values.get("reproduce")
    if reproduce_val is None:
        reproduce_val = False
    if not isinstance(reproduce_val, bool):
        raise PlanError(f"{source}: 'reproduce' must be true or false, got {reproduce_val!r}")

    format_val = values.get("format")
    if format_val is not None and not isinstance(format_val, str):
        raise PlanError(f"{source}: 'format' must be a string, got {format_val!r}")

    return Plan(
        format=format_val,
        approved_boundaries=as_list("approved_boundaries"),
        known_limits=as_list("known_limits"),
        reproduce=reproduce_val,
        approved_by=approved_by,
        ignored_keys=tuple(sorted(ignored)),
    )


def load_plan(path: Path) -> Plan:
    if not path.is_file():
        raise PlanError(f"{path}: no such plan file")
    return parse_plan_text(path.read_text(encoding="utf-8"), source=str(path))
