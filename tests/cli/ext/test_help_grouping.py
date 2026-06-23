# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Top-level ``axi ext --help`` lifecycle grouping."""

from __future__ import annotations

import pytest

from axiom.extensions.cli import LIFECYCLE_GROUPS, get_parser


@pytest.fixture
def help_text() -> str:
    return get_parser().format_help()


class TestLifecycleGrouping:
    """Each lifecycle header + a representative verb from its group appears."""

    def test_all_populated_headers_present(self, help_text: str):
        # Diagnostic (whoami/status/completion) is added by later units — its
        # header only appears once any of those verbs ship.
        always_on = {"Scaffold", "Iterate", "Publish", "Consume", "Legacy"}
        for header, _ in LIFECYCLE_GROUPS:
            if header in always_on:
                assert f"{header}:" in help_text, (
                    f"missing lifecycle header {header!r} in help output"
                )

    def test_scaffold_has_init(self, help_text: str):
        assert "init" in help_text

    def test_iterate_has_lint_and_test(self, help_text: str):
        assert "lint" in help_text
        assert "test" in help_text

    def test_publish_has_publish_and_sign(self, help_text: str):
        assert "publish" in help_text
        assert "sign" in help_text

    def test_consume_has_install_and_list(self, help_text: str):
        assert "install" in help_text
        assert "list" in help_text

    def test_legacy_has_check_and_mcp(self, help_text: str):
        assert "check" in help_text
        assert "mcp" in help_text

    def test_verbs_appear_under_their_group_headers(self, help_text: str):
        """Each verb listed in LIFECYCLE_GROUPS appears *after* its header."""
        for header, verbs in LIFECYCLE_GROUPS:
            header_pos = help_text.find(f"{header}:")
            if header_pos == -1:
                continue
            # Find the next lifecycle header after this one to scope the check.
            next_header_pos = len(help_text)
            for other_header, _ in LIFECYCLE_GROUPS:
                if other_header == header:
                    continue
                p = help_text.find(f"{other_header}:", header_pos + 1)
                if p != -1 and p < next_header_pos:
                    next_header_pos = p
            block = help_text[header_pos:next_header_pos]
            # At least one verb from the group should appear in its block.
            # (Some groups — e.g., Diagnostic — are populated later in the
            # batch; we tolerate zero matches for those.)
            matches = [v for v in verbs if f"  {v}" in block]
            if header in {"Scaffold", "Iterate", "Publish", "Consume", "Legacy"}:
                assert matches, (
                    f"no verbs from {header} group found in its block"
                )
