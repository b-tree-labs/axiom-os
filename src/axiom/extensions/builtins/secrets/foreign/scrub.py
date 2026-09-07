# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Post-rotation scrub report — known plaintext location TYPES.

After a rotation the superseded value may still sit in plaintext on
surfaces outside this repo's jurisdiction. Policy (issue #667): we
REPORT the location types with remediation hints; we never auto-edit
files outside the repository, and we never scan file contents for the
value (that would require holding the value against arbitrary files).
"""

from __future__ import annotations


def scrub_candidates(name: str) -> list[dict]:
    """Location types where a foreign credential historically leaks.

    Every entry carries a human hint; nothing here reads or edits the
    listed locations.
    """
    return [
        {
            "location_type": "harness-settings-allow-rules",
            "example_paths": [
                "~/.claude/settings.json",
                "~/.claude/settings.local.json",
                "<project>/.claude/settings.json",
            ],
            "hint": (
                f"grep the harness settings allow-rules for the old "
                f"{name} value and delete any rule that inlines it; "
                "re-allow using the credential-helper form instead"
            ),
        },
        {
            "location_type": "shell-rc-exports",
            "example_paths": ["~/.zshrc", "~/.bashrc", "~/.zshenv", "~/.envrc"],
            "hint": (
                "remove `export ...` lines carrying the old value; consumers "
                "should read via `axi secrets` / the git credential helper"
            ),
        },
        {
            "location_type": "memory-fragments",
            "example_paths": ["axi memory ledger", "auto-memory topic files"],
            "hint": (
                "run the secret-gate forget path (`axi memory forget`) for "
                "fragments that captured the old value"
            ),
        },
        {
            "location_type": "session-transcripts",
            "example_paths": ["harness transcript/session stores"],
            "hint": (
                "purge or age out transcripts that captured the old value; "
                "the rotation makes the captured token dead, but scrubbing "
                "limits secondary reuse of the pattern"
            ),
        },
        {
            "location_type": "git-config-inline-urls",
            "example_paths": ["~/.gitconfig", "<repo>/.git/config"],
            "hint": (
                "replace any `https://user:token@host` remote URL with a "
                "clean URL plus `axi secrets wire-git`"
            ),
        },
    ]


__all__ = ["scrub_candidates"]
