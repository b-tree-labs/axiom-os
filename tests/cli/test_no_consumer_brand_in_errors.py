# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Guard: the dispatcher's user-facing strings never hardcode a consumer brand.

Sandbox audit 2026-09-18 (gap 4, bonus defect): `axi search` failures printed
`neut: command 'search' failed: ...` — a consumer distribution's brand leaking
out of the domain-agnostic `axi` binary. Error prefixes must come from the
branding registry (the binary the operator actually typed), never a literal.

This is a source-level guard (the consumer-name-leak program): it fails if a
hardcoded `neut:`-prefixed user-facing string reappears in the dispatcher.
"""

from __future__ import annotations

import re
from pathlib import Path

import axiom


def _dispatcher_source() -> str:
    return (Path(axiom.__file__).parent / "axiom_cli.py").read_text(encoding="utf-8")


def test_no_hardcoded_neut_prefix_in_prints():
    src = _dispatcher_source()
    leaks = [
        line.strip()
        for line in src.splitlines()
        if re.search(r"""print\((?:f?)["']neut[: ]""", line)
    ]
    assert not leaks, f"hardcoded consumer brand in dispatcher output: {leaks}"


def test_no_hardcoded_neut_in_usage_hints():
    """`Did you mean: neut ...` / `Run 'neut --help'` must use branding too."""
    src = _dispatcher_source()
    leaks = [
        line.strip()
        for line in src.splitlines()
        if ("Did you mean: neut " in line) or ("'neut --help'" in line)
    ]
    assert not leaks, f"hardcoded consumer brand in usage hints: {leaks}"


def test_dispatch_error_prefix_uses_branding(capsys):
    """The missing-symbol failure path prints the active CLI name, not `neut`."""
    import pytest

    from axiom import axiom_cli

    with pytest.raises(SystemExit) as exc:
        axiom_cli._dispatch_extension(
            "explode",
            {"module": "axiom.rag", "function": "definitely_missing", "builtin": True},
        )
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "neut:" not in out
    # The active brand's CLI name fronts the message.
    from axiom.infra.branding import get_branding

    assert f"{get_branding().cli_name}:" in out
