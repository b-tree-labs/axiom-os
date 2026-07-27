# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Release lifecycle and CI/CD orchestration.

Houses **RIVET**, the build / ship / release agent in the platform's
REPL cycle (system service tier — supports the cycle without
participating in Read/Eval/Print). See ``agents/rivet/persona.md`` for
RIVET's role definition.

Handles:
- Semantic version bumping (major, minor, patch) — `axi release`
- Git tagging
- Pre-release validation (tests, lint, eval gates)
- Changelog generation from commit history
- CI pipeline monitoring (RIVET watcher; loop wiring pending)
- Failure-pattern matching across GitHub Actions / GitLab CI
"""

# Back-compat aliases for the pre-2026-05-30 module names. The
# `agent_cli` module became `_legacy_rivet_cli` (rivet handlers);
# `cli` was the simple release-bump module, now `_legacy_release_cli`,
# with `cli` re-purposed as the consolidated thin dispatcher.
#
# Tests reaching for `agent_cli` get the legacy rivet handlers; tests
# reaching for the pre-migration `cli` (release-bump) get the renamed
# legacy module. Code touching either path during the transition
# continues to work.
from . import _legacy_rivet_cli as agent_cli  # noqa: E402,F401
from . import _legacy_release_cli as _legacy_release_module  # noqa: E402,F401

__all__ = ["agent_cli"]
