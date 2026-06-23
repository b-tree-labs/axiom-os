# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Provider protocol + CliContext for ``axi ext`` verbs.

Each ``axi ext <verb>`` is implemented as an :class:`ExtCliProvider`. Axiom
ships default built-in providers; third parties may override any verb by
registering an entry point in the ``axiom.ext.cli.providers`` group (see
AEOS §11.3).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass
class CliContext:
    """Runtime context passed to every provider's ``run`` method.

    Attributes:
        cwd: The process working directory when ``axi`` was invoked.
        extension_path: Resolved path to the extension the verb should act on.
            For verbs that take an optional ``[<path>]`` argument, the
            dispatcher populates this by resolving the argument relative to
            ``cwd`` and defaults to ``cwd`` when the argument is omitted.
        config: Caller-supplied configuration blob (typically loaded from
            ``~/.axiom/config.toml`` or the equivalent per-product file).
            Providers must treat missing keys as "use a sensible default".
        extras: Free-form dictionary for harness-specific extras (registry
            URL overrides, cached catalogs, etc.). Empty by default.
    """

    cwd: Path
    extension_path: Path | None = None
    config: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.extension_path is None:
            self.extension_path = self.cwd


@runtime_checkable
class ExtCliProvider(Protocol):
    """Provider interface for an ``axi ext <verb>`` command.

    Implementations provide:

    - ``verb`` — the CLI verb (e.g. ``"init"``, ``"lint"``)
    - ``description`` — one-line help string
    - ``add_arguments(parser)`` — register verb-specific flags
    - ``run(args, context)`` — execute the verb, return a POSIX exit code
    """

    verb: str
    description: str

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Register flags specific to this verb on ``parser``."""

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        """Execute the verb. Return 0 on success, non-zero on failure."""


__all__ = ["CliContext", "ExtCliProvider"]
