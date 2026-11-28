# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The repo-wide guard tests CI runs before the long suite (#2441 item 1).

A guard test reads the whole tree: the retired word, a customer name in the
platform, a stray root directory, a workflow that stopped being a bare pytest.
No change's selected tests include it, so it surfaces wherever the full run
happens to reach it. On 2026-11-25 the retired-word guard failed 14 minutes into
a 20-minute run, at 98%, and it needs five seconds.

The Smoke job runs exactly this list, in process, right after the install and
before it compiles or collects anything. The unit job needs Smoke, so a red
guard means the long suite never starts. They stay in the full suite as well:
the "Run unit tests" step is a bare pytest (``test_every_test_file_is_collected``).

This is not ``guards.py``. Those are the pre-push guards, run with ``--noconftest``
and no plugins so a bare server can run them. These are ordinary tests that need
the package installed, which Smoke has.

    python -m axiom.extensions.builtins.release.ci_guards --ci-targets

A guard is added here with the reason it must run first. Keep the list to tests
that take seconds and need no database: Smoke is a gate, not a second suite.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

CI_GUARD_TESTS: dict[str, str] = {
    # A pattern, not a name: every retired word has its guard (docs/conventions/terminology-ledger.md),
    # and writing one here would be a use of the word it guards. The first failed a full run at 98%
    # on 2026-11-25, and this file's own first draft was caught by it.
    "tests/test_*_is_retired.py": (
        "a retired word cannot spread, and each retired word has a guard named for it"
    ),
    "tests/test_axiom_names_no_domain.py": (
        "the platform names no customer and learns no consumer's vocabulary"
    ),
    "tests/test_repo_hygiene.py": (
        "no stray root directory, no runtime data in src, no stale imports"
    ),
    "tests/test_adr_numbering.py": "an ADR number is an identifier other documents cite",
    "tests/test_every_test_file_is_collected.py": (
        "a test file outside the collection paths proves nothing, and the unit job must stay bare"
    ),
    "tests/test_ci_cost_controls.py": (
        "the workflows keep their cost controls and the contracts other guards read"
    ),
    "tests/test_ci_fails_fast.py": "CI keeps the shape that finds a failure early",
    "tests/test_release_fast_path_in_ci.py": (
        "the release fast path skips the suite only for a version-only commit on a green parent"
    ),
    "tests/infra/test_ci_artifact_retention.py": (
        "a 90-day default artifact fills the Actions storage cap"
    ),
    "tests/infra/test_skill_chain_lint.py": (
        "a skill with no declared risk reaches MCP and chat unreviewed"
    ),
    "tests/test_declared_services_resolve.py": "a declared service must name something real",
    "tests/test_migrations_expand_before_they_contract.py": (
        "a migration that contracts before it expands breaks a running node"
    ),
    "tests/test_secrets_are_never_echoed.py": "a secret printed once is exposed and must be rotated",
}


def _root() -> Path:
    return Path(__file__).resolve().parents[5]


def targets(root: Path | None = None) -> tuple[str, ...]:
    """The pytest targets, in the order they run. A pattern expands to the files it matches."""
    base = root or _root()
    found: list[str] = []
    for entry in CI_GUARD_TESTS:
        if "*" in entry:
            found.extend(sorted(path.relative_to(base).as_posix() for path in base.glob(entry)))
        else:
            found.append(entry)
    return tuple(dict.fromkeys(found))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m axiom.extensions.builtins.release.ci_guards")
    parser.add_argument("--ci-targets", action="store_true", required=True)
    parser.parse_args(argv)
    print("\n".join(targets()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
