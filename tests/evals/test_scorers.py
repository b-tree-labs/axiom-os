# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Core scorers: exact_match, contains, json_schema_valid."""

from __future__ import annotations


def test_exact_match() -> None:
    from axiom.evals.scorers import exact_match

    assert exact_match("hi", "hi") == 1.0
    assert exact_match("hi", "bye") == 0.0
    assert exact_match(" hi ", "hi") == 1.0  # stripped


def test_contains() -> None:
    from axiom.evals.scorers import contains

    assert contains("the answer is 42", "42") == 1.0
    assert contains("the answer is 42", "43") == 0.0
    assert contains("HELLO world", "hello") == 1.0  # case insensitive


def test_json_schema_valid() -> None:
    from axiom.evals.scorers import json_schema_valid

    assert json_schema_valid('{"a": 1}', expected={"required_keys": ["a"]}) == 1.0
    assert json_schema_valid('{"a": 1}', expected={"required_keys": ["b"]}) == 0.0
    assert json_schema_valid("not json", expected={"required_keys": []}) == 0.0
