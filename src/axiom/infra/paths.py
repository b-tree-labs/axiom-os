# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Runtime path helpers for Axiom.

Two distinct path concepts live here:

  project_dir_name()     — the project-local state directory name, e.g. ``.axi``
  get_project_root()     — the current *project's* root directory (.git / dot-dir anchor)
  get_project_state_dir()— project-local state, e.g. ``<root>/.axi/``
  get_user_state_dir()   — user-global state, e.g. ``~/.axi/``

Never derive runtime paths from ``axiom.__init__.REPO_ROOT``.  That value
is resolved at import time relative to the installed package's ``__file__``,
which points inside ``site-packages/`` for wheel installs.  Use these helpers
instead — they resolve relative to the *working directory* and active branding.

Project-local directories (e.g. ``<root>/.axi/publisher/``) should be anchored
with ``get_project_state_dir() / ...``.  User-global directories (credentials,
settings, services) should be anchored with ``get_user_state_dir() / ...``.
Neither name is ever spelled as a literal: both come from the active branding's
``cli_name``, so a product built on the platform gets its own isolated tree and
the platform never carries a consumer's name.  The directory a consumer name
used to produce is retired, and :func:`axiom.infra.brand_migration.warn_if_legacy_dir`
tells an operator who still has one.
"""

from __future__ import annotations

import os
from pathlib import Path

from axiom.infra.branding import get_branding


def project_dir_name() -> str:
    """Return the project-local state directory name for the active branding.

    ``.axi`` for the platform, ``.<cli_name>`` for a product that registered its
    own branding.  Same rule :func:`get_user_state_dir` applies to the
    user-global tree, so the two halves of a product's state agree on a name.
    """
    return f".{get_branding().cli_name}"


def get_project_root(start: Path | None = None) -> Path:
    """Return the project root directory for the current working context.

    Resolution order:
    1. ``AXIOM_ROOT`` environment variable (explicit override — always wins)
    2. Walk up from *start* (default: ``Path.cwd()``) looking for ``.git``
       or the branded project directory (:func:`project_dir_name`) — returns
       the first directory that has either.
    3. Fall back to *start* (i.e. ``cwd``) if no anchor is found.

    A directory anchored only on the retired consumer-named dot-directory is no
    longer a project root.  Rather than walking silently past it, the fallback
    path reports it once, naming both directories, so an operator learns why
    their project root moved.

    This is safe for both editable (``pip install -e .``) and wheel installs
    because it never reads ``__file__``.
    """
    env_root = os.environ.get("AXIOM_ROOT")
    if env_root:
        return Path(env_root).resolve()

    base = (start or Path.cwd()).resolve()
    dot_dir = project_dir_name()
    for candidate in [base, *base.parents]:
        if (candidate / ".git").exists() or (candidate / dot_dir).exists():
            return candidate

    # No anchor at all. If a retired one is sitting there, say so rather than
    # letting the project root quietly become the working directory.
    from axiom.infra.brand_migration import warn_if_legacy_dir

    for candidate in [base, *base.parents]:
        warn_if_legacy_dir(candidate / dot_dir)
    return base


def get_project_state_dir(root: Path | None = None) -> Path:
    """Return ``<project root>/.<cli_name>`` — the project-local state directory.

    Every project-local path the platform reads or writes hangs off this, so
    the directory name is decided in one place.  The directory is NOT created
    here: several callers want to test for its presence.  A retired
    consumer-named directory sitting beside a missing current one is reported
    once, so publisher state and credentials left there do not just stop being
    found.
    """
    from axiom.infra.brand_migration import warn_if_legacy_dir

    state_dir = (root if root is not None else get_project_root()) / project_dir_name()
    warn_if_legacy_dir(state_dir)
    return state_dir


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
    * A product built on it  →  ``~/.<its cli_name>/``

    Honors the ``AXI_STATE_DIR`` env-var override so tests and isolated
    runtimes can redirect state without touching the user's real state tree.
    That name is a fixed literal on purpose: it used to be spelled from the
    active branding, which meant the variable an operator had to set changed
    with whichever product registered last.  A runbook cannot name a moving
    variable, so the *directory* follows branding and the *variable* does not.

    The directory is created on first access.  Its name comes from the active
    branding's ``cli_name`` field, so products automatically get their own
    isolated state tree without any code changes.
    """
    from axiom.infra.brand_migration import getenv, warn_if_legacy_dir

    override = getenv("AXI_STATE_DIR")
    if override:
        state_dir = Path(override)
    else:
        state_dir = Path.home() / project_dir_name()
        warn_if_legacy_dir(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir
