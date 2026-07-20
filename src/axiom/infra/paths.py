# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Runtime path helpers for Axiom.

Two distinct path concepts live here:

  get_project_root()   — the current *project's* root directory (.git / .neut anchor)
  get_user_state_dir() — user-global state, e.g. ~/.axi/ or ~/.neut/

Never derive runtime paths from ``axiom.__init__.REPO_ROOT``.  That value
is resolved at import time relative to the installed package's ``__file__``,
which points inside ``site-packages/`` for wheel installs.  Use these helpers
instead — they resolve relative to the *working directory* and active branding.

Project-local directories (e.g. ``.neut/publisher/``) should be anchored with
``get_project_root() / ".neut" / ...``.  User-global directories (credentials,
settings, services) should be anchored with ``get_user_state_dir() / ...``.
"""

from __future__ import annotations

import os
from pathlib import Path

from axiom.infra.branding import get_branding


def get_project_root(start: Path | None = None) -> Path:
    """Return the project root directory for the current working context.

    Resolution order:
    1. ``AXIOM_ROOT`` environment variable (explicit override — always wins)
    2. Walk up from *start* (default: ``Path.cwd()``) looking for ``.git``
       or ``.neut`` — returns the first directory that has either.
    3. Fall back to *start* (i.e. ``cwd``) if no anchor is found.

    This is safe for both editable (``pip install -e .``) and wheel installs
    because it never reads ``__file__``.
    """
    env_root = os.environ.get("AXIOM_ROOT")
    if env_root:
        return Path(env_root).resolve()

    base = (start or Path.cwd()).resolve()
    for candidate in [base, *base.parents]:
        if (candidate / ".git").exists() or (candidate / ".neut").exists():
            return candidate
    return base


def get_agent_output_dir(agent_name: str) -> Path:
    """Return the per-agent runtime output directory for the current project.

    Resolves to ``<project_root>/runtime/agent-output/<agent_name>/``.
    The directory is created on first access.

    **Convention (AEOS default):** any extension agent writing
    operational output (heartbeat JSON, health reports, debug dumps,
    cron logs, …) should resolve its write path via this helper rather
    than picking a bespoke ``runtime/<something>/`` location. Consumers
    then need to ``.gitignore`` only the single
    ``runtime/agent-output/`` root to cover every agent forever.

    The motivating failure case was a domain consumer's `runtime/mo-reports/`
    accumulating 32MB of untracked heartbeat JSON because the consumer's
    ``.gitignore`` didn't enumerate the path the M-O agent had picked.

    Raises ValueError on agent names that could escape the
    ``agent-output/`` root or confuse downstream tooling: empty,
    relative segments (``"."`` / ``".."``), embedded slashes /
    backslashes / nulls.
    """
    if not isinstance(agent_name, str):
        raise ValueError(f"agent_name must be a string, got {type(agent_name).__name__}")
    if not agent_name:
        raise ValueError("agent_name must not be empty")
    if agent_name in (".", ".."):
        raise ValueError(f"agent_name must not be a relative segment, got {agent_name!r}")
    if any(ch in agent_name for ch in ("/", "\\", "\x00")):
        raise ValueError(
            f"agent_name must not contain path separators or null bytes, "
            f"got {agent_name!r}"
        )

    out = get_project_root() / "runtime" / "agent-output" / agent_name
    out.mkdir(parents=True, exist_ok=True)
    return out


def get_user_state_dir() -> Path:
    """Return the user-global state directory, branding-aware.

    * Axiom standalone   →  ``~/.axi/``
    * A domain consumer  →  e.g. ``~/.neut/``

    Honors ``AXI_STATE_DIR`` (and ``NEUT_STATE_DIR``) env-var overrides
    so tests and isolated runtimes can redirect state without touching
    the user's real state tree.

    The directory is created on first access.  Its name comes from the active
    branding's ``cli_name`` field, so domain products automatically get their
    own isolated state tree without any code changes.
    """
    import os

    cli_name = get_branding().cli_name
    override = os.environ.get(f"{cli_name.upper()}_STATE_DIR") or os.environ.get(
        "AXI_STATE_DIR"
    )
    if override:
        state_dir = Path(override)
    else:
        state_dir = Path.home() / f".{cli_name}"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir
