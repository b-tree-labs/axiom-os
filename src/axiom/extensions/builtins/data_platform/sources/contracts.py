# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``SourceKindProvider`` — the seam each ingest-source kind plugs into.

DP-1 ships one kind (Box). The dozens-to-hundreds that follow (Google
Drive, SharePoint, S3, GitHub repos, JIRA, Confluence, local FS, …)
register themselves through this protocol so the platform's
``axi data install`` / ``register`` surfaces stay source-agnostic.

The platform NEVER speaks any one source's vocabulary. A provider:

1. **declares its kind name** (used in `register <name> <kind>`),
2. **adds its own argparse args** to the CLI register-subcommand,
3. **validates** a populated ``ConnectorConfig`` before persistence,
4. **constructs** the runtime :class:`IngestSource` from a config.

A consumer extension or third-party package adds a kind by shipping a
provider module + registering it at import time (idiom: the package's
``__init__.py`` calls ``default_source_kind_registry().register(...)``).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..agents.plinth.connectors import ConnectorConfig
    from .ingest_source import IngestSource


# ---------------------------------------------------------------------------
# Preflight — kind-agnostic connection verification + plain-language fixes
# ---------------------------------------------------------------------------
#
# Every kind reports health the SAME way so one wizard/MCP/web surface can
# render it. A check is either ok, or carries a remediation a non-coder can
# act on (copyable value + a hint of who must do it). This turns the
# silent-crashloop-six-days-later failure mode into an instant checklist.


@dataclass(frozen=True)
class PreflightCheck:
    """One verification step's outcome."""

    name: str                      # short label, e.g. "Authentication"
    ok: bool
    message: str                   # plain-language result
    # Present only when ok is False: how to fix it.
    remediation: str = ""          # imperative, non-coder-readable
    copy_value: str = ""           # a value to copy verbatim (email, id, URL)
    actor: str = "you"             # "you" | "admin" — who must perform the fix


@dataclass(frozen=True)
class PreflightResult:
    """The full connection verification for one connector."""

    connector: str
    kind: str
    checks: list[PreflightCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def blockers(self) -> list[PreflightCheck]:
        return [c for c in self.checks if not c.ok]


@runtime_checkable
class SourceKindProvider(Protocol):
    """Self-describing kind provider — one per ingest-source flavor."""

    kind: str
    """Stable identifier the registry keys on; matches the
    ``ConnectorConfig.kind`` field (e.g. ``"box"``, ``"gdrive"``,
    ``"sharepoint"``). Lowercase, no spaces, kebab-case allowed."""

    description: str
    """One-line human description; shown in `axi data register --help`
    and `axi data list-kinds`."""

    def add_register_args(self, subparser: argparse.ArgumentParser) -> None:
        """Attach kind-specific CLI flags to the register subparser.

        The platform owns the positional ``name`` and the
        kind-agnostic ``--bronze-root`` / ``--default-disposition`` /
        ``--default-tier`` / ``--provenance-rules-file`` /
        ``--rag-dsn-env`` flags. The provider adds anything else its
        kind needs (e.g. for Box: ``--folder-id``, ``--session-path``).
        """
        ...

    def params_from_args(self, args: argparse.Namespace) -> dict[str, str]:
        """Map parsed argparse args → the kind-specific ``params`` dict
        that lands in :class:`ConnectorConfig.params`.

        Only kind-specific values go here (Box's ``folder_id``,
        ``session_state_b64``). The platform-generic fields are
        already populated by the CLI dispatcher.
        """
        ...

    def validate(self, config: ConnectorConfig) -> list[str]:
        """Validate a populated config; return a list of human errors.

        Empty list = valid. Called BEFORE the config is persisted and
        BEFORE the Dagster sensor would try to use it — catches
        kind-specific misconfiguration loudly at register time.
        """
        ...

    def construct(self, config: ConnectorConfig) -> IngestSource:
        """Build the runtime :class:`IngestSource` from a saved config.

        Called by the Dagster sensor (and PLINTH's run-ingest skill)
        when they need a live source to walk. The provider owns its
        client/session lifecycle.
        """
        ...

    def preflight(self, config: ConnectorConfig) -> PreflightResult:
        """Verify the connection live and return actionable checks.

        Optional. Authenticates with the configured credentials and
        confirms the target is reachable (e.g. the folder is visible),
        returning plain-language remediation for anything wrong. Run at
        register time and on demand so failures surface immediately
        instead of at the next sensor tick.
        """
        ...


__all__ = ["SourceKindProvider", "PreflightCheck", "PreflightResult"]
