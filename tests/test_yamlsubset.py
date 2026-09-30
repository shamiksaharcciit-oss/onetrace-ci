"""The restricted YAML reader behind the plan file.

Two properties, both checked here:

- anything it accepts, it reads exactly as a real YAML parser does (PyYAML, which LibCST
  already brings in, so this costs no new dependency), within the documented limits
  (no numbers: a plain scalar other than true/false/null stays the string as written);
- anything outside the subset is refused, naming the line, never guessed at.
"""
from __future__ import annotations

import pytest
import yaml

from onetrace_ci.yamlsubset import SubsetError, parse

#: Documents inside the subset. None of them holds a number, so PyYAML's own reading is the
#: expected value with no adjustment at all.
ACCEPTED = {
    "flat scalars": "approved_by: alice\nformat: onetrace-ci-plan/0.1\n",
    "block list": "known_limits:\n  - embed\n  - answer\n",
    "block list at the key's own indent": "known_limits:\n- embed\n- answer\n",
    "flow list": "approved_boundaries: [retrieve, answer]\n",
    "empty flow list": "approved_boundaries: []\n",
    "flow mapping": "instrument: {name: bm25, package: rank_bm25}\n",
    "empty flow mapping": "config: {}\n",
    "nested block mapping": "ci:\n  run: python -m pipeline\n  baseline: runs/baseline\n",
    "list of mappings": (
        "stages:\n"
        "  - name: intake\n"
        "    memory_inputs: [request]\n"
        "    trust: externally-sourced\n"
        "  - name: retrieve\n"
        "    function: pipeline.retrieval:retrieve\n"
        "    instrument: {name: bm25, package: rank_bm25}\n"
        "    inputs: [intake]\n"
        "    files: [data/corpus.json]\n"),
    "comments everywhere": (
        "# a whole-line comment\n"
        "entry: pipeline.main:run            # the function that is one run\n"
        "stages:   # trailing\n"
        "  - name: a   # after a scalar\n"
        "    rederivable: \"false\"   # after a quoted scalar\n"),
    "a hash inside a plain scalar is not a comment": "note: issue#12 stays\n",
    "a hash inside quotes is not a comment": "note: \"a # b\"\nother: 'c # d'\n",
    "quoted scalars": "a: \"hosted model; sampling not reproducible\"\nb: 'it''s'\nc: \"tab\\there\"\n",
    "booleans and null": "a: true\nb: false\nc: null\nd: ~\ne:\n",
    "braces inside a block plain scalar": "run_dir: runs/{run_id}\n",
    "a colon without a space stays in the scalar": "entry: pipeline.main:run\n",
    "quoted items in a flow list": "files: [\"data/a b.json\", 'c,d.txt']\n",
    "blank lines between keys": "a: x\n\n\nb: y\n",
    "flow list inside a list of mappings, at the dash": "stages:\n  - [a, b]\n",
    "no trailing newline": "approved_by: alice",
    "crlf line endings": "approved_by: alice\r\nknown_limits:\r\n  - embed\r\n",
    "a UTF-8 byte order mark": "﻿approved_by: alice\n",
    "a quoted value holding a comma in a flow mapping": "i: {k: \"x, y\", m: z, n: 'p, q'}\n",
    "a key with a space": "reviewed on: monday\n",
    #: A mapping keyed by entry (`pipeline.main:run`) needs a key holding a colon: quoted, it is
    #: a string to every reader.
    "a double-quoted key holding a colon": (
        "ci:\n  baseline:\n    \"pipeline.main:run\": runs/baseline\n    \"pipeline.ingest:run\": null\n"),
    "a single-quoted key": "'pipeline.main:run': python -m pipeline.demo\n",
    "a quoted key with a comment after its value": "\"a:b\": c   # a note\n",
    "a quoted key whose value is a block list": "\"pipeline.main:run\":\n  - x\n  - y\n",
    "a quoted key as a list item's first key": "items:\n  - \"a:b\": c\n    d: e\n  - 'x:y': z\n",
    "a quoted scalar as a list item stays a scalar": "items:\n  - \"a: b\"\n  - 'c'\n",
}


@pytest.mark.parametrize("name", sorted(ACCEPTED))
def test_accepted_documents_read_exactly_as_pyyaml_reads_them(name, examined):
    text = ACCEPTED[name]
    examined(1, f"accepted document: {name}")
    assert parse(text, source="<test>") == yaml.safe_load(text)


def test_the_accepted_corpus_is_not_empty(examined):
    examined(len(ACCEPTED), "accepted documents compared against PyYAML")


def test_a_number_stays_the_string_as_written(examined):
    """The one documented difference from PyYAML: no numbers. A plan never needs one, and a
    number read as an int would silently lose leading zeros and spelling."""
    examined(3, "plain scalars that PyYAML would read as numbers")
    assert parse("a: 3\nb: 007\nc: 1.50\n", source="<t>") == {"a": "3", "b": "007", "c": "1.50"}


@pytest.mark.parametrize("word", ["yes", "no", "on", "off", "Yes", "NO", "On", "OFF"])
def test_yaml_1_1_boolean_spellings_are_refused_unless_quoted(word, examined):
    """YAML 1.1 (PyYAML) reads these as booleans and YAML 1.2 reads them as strings. A plan must
    mean one thing to every reader, so unquoted they are refused; quoted they are strings."""
    examined(1, f"the unquoted spelling {word!r}")
    with pytest.raises(SubsetError, match=r"line 1\b.*quote"):
        parse(f"approved_by: {word}\n", source="<t>")
    assert parse(f'approved_by: "{word}"\n', source="<t>") == {"approved_by": word}


REFUSED = {
    "block scalar": ("approved_by: |\n  a block scalar\n", 1),
    "folded scalar": ("approved_by: >\n  a folded scalar\n", 1),
    "anchor": ("approved_by: &anchor value\n", 1),
    "alias": ("approved_by: *alias\n", 1),
    "tag": ("approved_by: !tag value\n", 1),
    "nested flow list": ("a: [x, [y]]\n", 1),
    "flow mapping inside a flow list": ("a: [x, {y: z}]\n", 1),
    "multi-line flow list": ("a: [x,\n  y]\n", 1),
    "duplicate key": ("a: x\nb: y\na: z\n", 3),
    "duplicate key in a flow mapping": ("a: {k: 1, k: 2}\n", 1),
    "tab indentation": ("a:\n\t- x\n", 2),
    "document marker": ("---\na: x\n", 1),
    "directive": ("%YAML 1.2\na: x\n", 1),
    "top-level list": ("- a\n- b\n", 1),
    "top-level scalar": ("not a key value line at all\n", 1),
    "indented first line": ("  leading_indent: true\n", 1),
    "a plain scalar with colon-space": ("a: b: c\n", 1),
    "unterminated double quote": ("a: \"open\n", 1),
    "unterminated single quote": ("a: 'open\n", 1),
    "text after a closing quote": ("a: \"x\" y\n", 1),
    "unknown escape": ("a: \"\\q\"\n", 1),
    "complex key": ("? a\n: b\n", 1),
    "a Unicode line separator hiding a key in a comment": ("approved_by: alice\n# signed off\u2028reproduce: true\n", 2),
    "a Unicode paragraph separator": ("a: x\u2029b: y\n", 1),
    "a next-line character": ("a: x\x85b: y\n", 1),
    "a NUL": ("a: x\x00\n", 1),
    "a DEL": ("a: x\x7f\n", 1),
    "a C1 control character": ("a: \x9b\n", 1),
    "a lone surrogate escape": ("a: \"\\ud800\"\n", 1),
    "a plain =": ("x: =\n", 1),
    "a tab after a colon in a plain scalar": ("x: a:\tb\n", 1),
    "a date": ("d: 1999-12-31\n", 1),
    "a boolean-like key": ("true: x\n", 1),
    "a null-like key": ("null: x\n", 1),
    "a numeric key": ("1: x\n", 1),
    "list item under a scalar": ("a: x\n  - y\n", 2),
    "mapping continuation at the wrong indent": ("stages:\n  - name: a\n   trust: x\n", 3),
    "a dash with no space": ("a:\n  -x\n", 2),
    "a quoted key in a flow mapping": ("a: {\"k:1\": x}\n", 1),
    "text after a quoted key": ("\"a\" b: c\n", 1),
    "a quoted key with no space after its colon": ("x: y\n\"a\":b\n", 2),
    "an empty quoted key": ("\"\": x\n", 1),
    "a duplicate quoted key": ("\"a:b\": x\n'a:b': y\n", 2),
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_outside_the_subset_is_refused_naming_the_line(name, examined):
    text, line = REFUSED[name]
    examined(1, f"refused document: {name}")
    with pytest.raises(SubsetError, match=rf"<test>: line {line}\b"):
        parse(text, source="<test>")


def test_every_refusal_case_is_also_not_silently_accepted_by_pyyaml_as_the_same_thing(examined):
    """A guard on the refusal list itself: where PyYAML *does* accept one of these, the subset's
    refusal is deliberate strictness, not an accident. Counted, so an empty list fails."""
    strict = 0
    for text, _ in REFUSED.values():
        try:
            yaml.safe_load(text)
            strict += 1
        except yaml.YAMLError:
            pass
    examined(len(REFUSED), "refusal cases checked against PyYAML")
    assert strict > 0
