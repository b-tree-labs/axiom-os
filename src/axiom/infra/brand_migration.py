# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The one module that still knows the names a downstream product left behind.

Axiom is the platform; a product built on it registers its own identity through
:mod:`axiom.infra.branding`. Platform code therefore never names a consumer, so
the platform can be handed to a different product without carrying this one's
name. An unfinished conversion left that rule broken in two places that are
real behaviour rather than prose: environment variables the platform read under
a consumer's prefix, and a consumer-named directory it anchored projects on.

This module is where that conversion ends. It holds the retired names, it is
the only place they appear in platform source, and the guard test in
``tests/infra/test_brand_migration.py`` keeps it that way.

Naming
------
Renamed variables take the prefix their own subsystem already uses:
``AXIOM_<SUBSYSTEM>_<THING>``, the dominant existing shape (``AXIOM_RAG_*``,
``AXIOM_BRIDGE_*``, ``AXIOM_SERVING_*``, ``AXIOM_INGEST_*``, ``AXIOM_GATE_*``).
``AXI_*`` stays reserved for the CLI-session and user-state surface, where
``AXI_STATE_DIR``, ``AXI_SESSION_ID`` and ``AXI_WORKSPACE_ROOT`` already live.
Where a direct sibling exists in the same package its exact shape is copied
rather than derived: ``AXIOM_ONEDRIVE_SESSION_DIR`` next door is why the Box
and Teams session directories are ``AXIOM_BOX_SESSION_DIR`` and
``AXIOM_TEAMS_SESSION_DIR``.

The names are literals, never built from :func:`~axiom.infra.branding.get_branding`.
Operators and runbooks need a name that is stable and greppable; a name that
moves with whichever product registered last is not one. The rule being
enforced is that the platform must not use a *consumer's* name, not that it
must have no name of its own.

Migration stance
----------------
House rule here is no backward-compatibility shims before launch, so the
platform name is the only one that works: nothing falls back to the retired
name, and its value is never read. A silent break is worse than a loud one
though, so a retired name that is set does not pass unremarked:

* **A retired name set, its replacement not** — one notice naming both names,
  on stderr and through the platform logger. Stderr is the channel an operator
  on a server actually sees; the logger is the channel an incident snapshot
  keeps. Neither carries the value.
* **A retired name that carries a credential, its replacement not set** —
  :class:`LegacyEnvVarError` instead of a notice. A warning that scrolls past
  leaves the platform making network calls without the credential the operator
  believes is in force, and an endpoint that accepts an unauthenticated request
  is the worst outcome available. Refusing to start is the smaller harm.
* **Both names set** — the replacement wins and the run continues, with one
  notice saying the retired name is ignored. Nothing is unauthenticated here,
  so nothing is fatal.

The on-disk anchor
------------------
:data:`LEGACY_PROJECT_DIR` is the retired project-local directory.
:func:`axiom.infra.paths.project_dir_name` supplies the current one from
``branding.cli_name``, which is what :func:`~axiom.infra.paths.get_user_state_dir`
already did for the user-global tree, so a downstream product keeps its own
directory and the platform gets ``.axi``. Finding a retired directory where the
current one is missing draws the same one-time notice, for the same reason:
an operator whose publisher state and credentials live there must not have them
quietly stop being found.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: The retired environment-variable prefix, as a whole-token pattern. The guard
#: hunts for this in non-test source.
CONSUMER_ENV_RE = re.compile(r"\bNEUT_[A-Z0-9_]+")

#: The retired project-local directory, as a path segment. Anchored so an
#: identifier that merely contains the name (``tools.neut_cli``) and a longer
#: word (``.neutron``) are not mistaken for it.
CONSUMER_DIR_RE = re.compile(r"(?<![\w.])\.neut(?![\w])")

#: The retired project-local directory name itself.
LEGACY_PROJECT_DIR = ".neut"


class LegacyEnvVarError(RuntimeError):
    """A retired credential variable is set and its replacement is not.

    Raised rather than warned because the alternative is a network call made
    without the credential the operator supplied.
    """


@dataclass(frozen=True)
class LegacyEnvVar:
    """A retired environment-variable name and how loudly to refuse it."""

    legacy: str
    #: True when the value is a credential, which makes an ignored setting a
    #: security problem rather than a configuration one.
    secret: bool
    #: What the variable configures, so a reviewer can check the mapping.
    reason: str


#: Platform name → the retired name it replaced. This is the *whole* inventory
#: of consumer-named variables the platform ever read from the environment;
#: adding an entry is a deliberate act, and the guard test fails on a consumer
#: name that appears anywhere else in non-test source.
LEGACY_ENV_VARS: dict[str, LegacyEnvVar] = {
    # axiom/rag — siblings AXIOM_RAG_DSN, AXIOM_RAG_GATEWAY_URL/_KEY, AXIOM_RAG_GEN_MODEL.
    "AXIOM_RAG_EMBED_URL": LegacyEnvVar(
        "NEUT_EMBED_URL", False, "base URL of an OpenAI-compatible embedding server"
    ),
    "AXIOM_RAG_EMBED_MODEL": LegacyEnvVar(
        "NEUT_EMBED_MODEL", False, "embedding model name to request"
    ),
    "AXIOM_RAG_EMBED_KEY": LegacyEnvVar(
        "NEUT_EMBED_KEY", True, "API key for the remote embedding endpoint"
    ),
    # builtins/install — the environment an install manifest is applied against.
    "AXIOM_INSTALL_ENV": LegacyEnvVar(
        "NEUT_ENV", False, "install-manifest environment name override"
    ),
    # builtins/publishing + data_platform — sibling AXIOM_ONEDRIVE_SESSION_DIR.
    "AXIOM_BOX_SESSION_DIR": LegacyEnvVar(
        "NEUT_BOX_SESSION_DIR", False, "Box browser session directory"
    ),
    # builtins/signals — same shape as the OneDrive sibling.
    "AXIOM_TEAMS_SESSION_DIR": LegacyEnvVar(
        "NEUT_TEAMS_SESSION_DIR", False, "Teams browser session directory"
    ),
    # builtins/hygiene — TIDY's scratch space, read by that extension alone.
    "AXIOM_HYGIENE_SCRATCH_DIR": LegacyEnvVar(
        "NEUT_SCRATCH_DIR", False, "TIDY scratch base directory"
    ),
    # builtins/update — the package index the self-update check queries. NOT
    # AXIOM_REGISTRY_URL: that name is taken by the extension registry in
    # axiom/cli/ext, which accepts file:// URLs only, and folding a package
    # index into it would break that check.
    "AXIOM_UPDATE_REGISTRY_URL": LegacyEnvVar(
        "NEUT_REGISTRY_URL", False, "package index queried by the update check"
    ),
    "AXIOM_UPDATE_REGISTRY_TOKEN": LegacyEnvVar(
        "NEUT_REGISTRY_TOKEN", True, "access token for that package index"
    ),
    # axiom/infra/subscribers — the GitLab side is GITLAB_URL/GITLAB_TOKEN
    # (vendor-generic); the project path is platform configuration.
    "AXIOM_GITLAB_PROJECT": LegacyEnvVar(
        "NEUT_GITLAB_PROJECT", False, "GitLab project path issues are filed on"
    ),
    # axiom/infra/axiom_logging — the forensic ring and its snapshots.
    "AXIOM_LOG_RING_CAPACITY": LegacyEnvVar(
        "NEUT_LOG_RING_CAPACITY", False, "records held in the forensic ring buffer"
    ),
    "AXIOM_LOG_FORENSIC_DIR": LegacyEnvVar(
        "NEUT_LOG_FORENSIC_DIR", False, "directory incident snapshots are written to"
    ),
    "AXIOM_LOG_SNAPSHOT_COOLDOWN_S": LegacyEnvVar(
        "NEUT_LOG_SNAPSHOT_COOLDOWN_S", False, "seconds between incident snapshots"
    ),
    # axiom/infra/paths — AXI_STATE_DIR already existed; what it replaces is the
    # branding-derived spelling get_user_state_dir used to accept.
    "AXI_STATE_DIR": LegacyEnvVar("NEUT_STATE_DIR", False, "user-global state directory override"),
}

#: Files still carrying a consumer name in non-test source, path (relative to
#: ``src/``) → why, naming the item that removes it. The guard test fails on any
#: file outside this list and on any entry here whose call site is gone.
CONSUMER_NAME_EXEMPT: dict[str, str] = {
    # The map above. The retired names have to be spelled somewhere for the
    # notice to name them; this is that somewhere, and the guard's companion
    # test asserts no other module spells one.
    "axiom/infra/brand_migration.py": (
        "the retired-name map itself; removed when the notices are retired"
    ),
    # Out of scope by instruction: another item owns this file region right now
    # and rewriting the same lines would collide with it.
    "axiom/extensions/builtins/chat/connections.py": (
        "under chat/, owned by the live chat item; that item removes it"
    ),
}


# ---------------------------------------------------------------------------
# Notices — once per name per process
# ---------------------------------------------------------------------------

_notified: set[str] = set()


def reset_notices() -> None:
    """Forget which notices have been emitted. For tests."""
    _notified.clear()


def _notice(key: str, message: str) -> None:
    """Emit *message* once per *key*, to stderr and to the platform logger.

    Two channels on purpose. The platform installs no console handler on its
    logger, so a lone ``log.warning`` can reach an in-memory ring and nothing
    else; stderr is what an operator running a command actually sees. The log
    line is what an incident snapshot preserves afterwards.
    """
    if key in _notified:
        return
    _notified.add(key)
    print(message, file=sys.stderr)
    log.warning("%s", message)


def getenv(name: str, default: str | None = None) -> str | None:
    """Read environment variable *name*, refusing the name it replaced.

    Behaves like ``os.environ.get(name, default)`` for any name with no retired
    predecessor. For a name in :data:`LEGACY_ENV_VARS` the retired spelling is
    never read as a value; it is only ever detected, so that an operator who set
    it is told rather than silently ignored.

    Args:
        name: The platform variable name.
        default: Returned when *name* is not set in the environment.

    Returns:
        The value of *name*, or *default*. An empty string counts as set: an
        operator writing ``AXIOM_RAG_EMBED_URL=""`` means "off", not "absent".

    Raises:
        LegacyEnvVarError: *name* is unset, and the credential-bearing variable
            it replaced is set. Continuing would run without that credential.
            The message names both variables and never the value.
    """
    spec = LEGACY_ENV_VARS.get(name)
    is_set = name in os.environ

    if spec is not None and spec.legacy in os.environ:
        if is_set:
            _notice(
                f"{spec.legacy}:superseded",
                f"{spec.legacy} is set and is ignored: the platform reads {name}, "
                f"which is also set and is what takes effect. Unset {spec.legacy}.",
            )
        elif spec.secret:
            raise LegacyEnvVarError(
                f"{spec.legacy} is set but the platform no longer reads it, and "
                f"its replacement {name} is unset. This value is a credential "
                f"({spec.reason}), so continuing would run unauthenticated. "
                f"Set {name} to the same value and unset {spec.legacy}."
            )
        else:
            _notice(
                f"{spec.legacy}:retired",
                f"{spec.legacy} is set but the platform no longer reads it. "
                f"Set {name} instead ({spec.reason}). "
                f"Until then this setting has no effect.",
            )

    return os.environ[name] if is_set else default


def warn_if_legacy_dir(current: Path) -> None:
    """Say so, once, when the retired directory is on disk and *current* is not.

    *current* is the directory the platform now uses. When a product's own
    branding makes the retired name the current one, there is nothing to
    report and nothing is emitted.
    """
    if current.name == LEGACY_PROJECT_DIR:
        return
    legacy = current.parent / LEGACY_PROJECT_DIR
    if not legacy.is_dir() or current.exists():
        return
    _notice(
        f"dir:{legacy}",
        f"{legacy} exists but the platform no longer reads it: project state now "
        f"lives in {current.name}/ ({current}). Move the contents across; nothing "
        f"is read from {LEGACY_PROJECT_DIR}/ any more.",
    )


__all__ = [
    "CONSUMER_DIR_RE",
    "CONSUMER_ENV_RE",
    "CONSUMER_NAME_EXEMPT",
    "LEGACY_ENV_VARS",
    "LEGACY_PROJECT_DIR",
    "LegacyEnvVar",
    "LegacyEnvVarError",
    "getenv",
    "reset_notices",
    "warn_if_legacy_dir",
]
