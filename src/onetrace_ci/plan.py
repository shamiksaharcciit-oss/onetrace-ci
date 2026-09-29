"""`onetrace-plan.yaml`: the plan file, as the gate reads it.

WHY A HAND-ROLLED READER, NOT PyYAML
-------------------------------------
A plan file carries a person's approval, so it must mean exactly one thing. The file is read
by `onetrace_ci.yamlsubset`, a deliberately restricted YAML reader: block mappings and lists,
one-line flow lists and mappings of scalars, quoted and plain scalars. It refuses anything it
cannot represent unambiguously (a block scalar, an anchor or alias, a tag, a nested flow
collection, a duplicate key), naming the line, rather than guess at a real YAML document's
full grammar. Anything it accepts, it reads exactly as a real YAML parser does, except that it
never produces a number. See that module for the full subset.

THE FIVE KEYS THE GATE READS, AND NOTHING ELSE READ SILENTLY
--------------------------------------------------------------
`format`, `approved_boundaries`, `known_limits`, `reproduce`, `approved_by`.
The keys `onetrace-ci instrument` reads (`entry`, `run_dir`, `stages`, `ci`) belong to the
same file and are not the gate's concern. Any other top-level key is ignored for gating
purposes, but named in the summary a gate run writes: a typo in a key this schema does not
know about must be visible, not silently dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from onetrace_ci.yamlsubset import SubsetError, parse

_GATE_KEYS = frozenset({"format", "approved_boundaries", "known_limits",
                        "reproduce", "approved_by"})
#: Read by `onetrace-ci instrument` (see `instrument_plan.py`), not by the gate.
INSTRUMENT_KEYS = frozenset({"entry", "run_dir", "stages", "ci"})
_KNOWN_KEYS = _GATE_KEYS | INSTRUMENT_KEYS


class PlanError(RuntimeError):
    """The plan file could not be read at all -- outside the supported
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


def names_nobody(value) -> bool:
    """`None`, `null` or `~` written as an approver name nobody (YAML reads `None` as text)."""
    return isinstance(value, str) and value.strip().lower() in ("none", "null", "~")


def read_document(text: str, *, source: str) -> dict:
    """The whole plan document as plain Python values, or `PlanError` naming the line."""
    try:
        return parse(text, source=source)
    except SubsetError as e:
        raise PlanError(str(e)) from None


def _name_list(values: dict, key: str, source: str) -> tuple[str, ...]:
    v = values.get(key)
    if v is None:
        return ()
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise PlanError(f"{source}: {key!r} must be a list of stage names, got {v!r}")
    return tuple(v)


def parse_plan_text(text: str, *, source: str) -> Plan:
    values = read_document(text, source=source)

    approved_by = values.get("approved_by")
    if approved_by is None or names_nobody(approved_by):
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
        approved_boundaries=_name_list(values, "approved_boundaries", source),
        known_limits=_name_list(values, "known_limits", source),
        reproduce=reproduce_val,
        approved_by=approved_by,
        ignored_keys=tuple(sorted(k for k in values if k not in _KNOWN_KEYS)),
    )


def load_plan(path: Path) -> Plan:
    if not path.is_file():
        raise PlanError(f"{path}: no such plan file")
    return parse_plan_text(path.read_text(encoding="utf-8"), source=str(path))
