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

One node has ONE settings store, whichever CLI name was typed (#2010). Both
folder names come from :class:`~axiom.infra.branding.BrandingConfig`'s default
``cli_name``, never from the *registered* branding: a product's CLI name brands
the command and the text people read, it does not split the state. It used to,
and a setting made through one CLI name was not seen through another on the same
node; inside one product process the state even moved between extension
discovery (before the product registered) and the command itself (after). A
folder a product's CLI name used to produce is brought across with
``axi state reconcile``.
"""

from __future__ import annotations

import os
from pathlib import Path


def project_dir_name() -> str:
    """Return the state directory name, ``.axi``, under every CLI name.

    Taken from :class:`~axiom.infra.branding.BrandingConfig`'s default rather
    than the registered branding, so it is the same before and after a product
    registers its own CLI name, and the same for every CLI name on the node
    (#2010). :func:`get_user_state_dir` uses the same name for the user-global
    tree, so the two halves of the state agree.
    """
    from axiom.infra.branding import BrandingConfig

    return f".{BrandingConfig.cli_name}"


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

    # No anchor at all. A retired one sitting there is worth reporting, but not
    # from here: this runs on nearly every code path. `axi doctor` reports it,
    # and `axi state reconcile` brings its contents across.
    return base


def get_project_state_dir(root: Path | None = None) -> Path:
    """Return ``<project root>/.axi`` — the project-local state directory.

    Every project-local path the platform reads or writes hangs off this, so
    the directory name is decided in one place.  The directory is NOT created
    here: several callers want to test for its presence.  A retired
    consumer-named directory sitting beside a missing current one is reported
    once, so publisher state and credentials left there do not just stop being
    found.
    """
    state_dir = (root if root is not None else get_project_root()) / project_dir_name()
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


def get_platform_state_dir() -> Path:
    """The platform's state directory: the same folder as :func:`get_user_state_dir`.

    It existed because the user state directory used to follow the registered
    CLI name, which was wrong for machine state such as CLI failure diagnoses:
    one alias warned about pending diagnoses and another, on the same install,
    reported none. Since #2010 every CLI name shares one store, so the two
    names resolve to one folder; both are kept so callers need not change.
    """
    return get_user_state_dir()


def get_user_state_dir() -> Path:
    """Return the user-global state directory, ``~/.axi/``, under every CLI name.

    A product's CLI name brands the command, not the state: a setting made
    through one CLI name is read through any other on the same node (#2010).

    Honors the ``AXI_STATE_DIR`` env-var override so tests and isolated
    runtimes can redirect state without touching the user's real state tree.
    That name is a fixed literal on purpose: a runbook cannot name a variable
    that moves with whichever product registered last.

    The directory is created on first access.
    """
    from axiom.infra.brand_migration import getenv

    override = getenv("AXI_STATE_DIR")
    if override:
        state_dir = Path(override)
    else:
        state_dir = Path.home() / project_dir_name()
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def get_cli_events_log() -> Path:
    """The CLI's event log: ``<user state dir>/logs/cli_events.jsonl``.

    Every ``axi`` invocation appends to it, so it must not depend on where the
    command was typed. It used to hang off :func:`get_project_root`, which is
    the nearest ``.git`` above the working directory, so running ``axi`` in
    somebody's repository wrote ``runtime/logs/cli_events.jsonl`` (and a
    ``.lock``) into that repository, and a hygiene check littered the repo it
    was checking (#1520). The log describes this user's CLI, not the project.
    """
    return get_user_state_dir() / "logs" / "cli_events.jsonl"

