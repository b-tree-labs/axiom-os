# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`data compare` from a terminal reaches its skill with everything the skill requires.

The skill has always required a bucket, and the command had no way to pass one, so
every `compare` typed at a terminal answered "missing required param(s): bucket".
"""

from __future__ import annotations

from axiom.extensions.builtins.data_platform import cli


def _params(argv: list[str]) -> dict:
    return cli._args_to_params(cli._parser().parse_args(argv))


def test_compare_passes_its_bucket_and_window():
    p = _params(["compare", "--role", "fluid_temperature", "--bucket", "1 hour",
                 "--from", "2026-10-01T00:00:00Z", "--to", "2026-10-02T00:00:00Z"])
    assert p["role"] == "fluid_temperature"
    assert p["bucket"] == "1 hour"
    assert p["window"]["start"] == "2026-10-01T00:00:00Z"
    assert p["window"]["end"] == "2026-10-02T00:00:00Z"


def test_compare_accepts_the_other_read_verbs_spelling_of_a_window():
    p = _params(["compare", "--role", "fluid_temperature", "--bucket", "1 hour",
                 "--start", "2026-10-01T00:00:00Z", "--end", "2026-10-02T00:00:00Z"])
    assert p["window"]["start"] == "2026-10-01T00:00:00Z"
