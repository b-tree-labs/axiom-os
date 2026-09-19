# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Discovery of :class:`ExtCliProvider` instances.

Order of precedence:

1. Providers registered via the ``axiom.ext.cli.providers`` entry-point group
   (third-party overrides) — these win.
2. Built-in providers shipped in :mod:`axiom.cli.ext.commands`.

Each discovered provider must satisfy the :class:`ExtCliProvider` protocol.
Duplicates from entry points replace built-ins with the same verb.
"""

from __future__ import annotations

import importlib.metadata as _md
from collections.abc import Callable

from axiom.cli.ext.provider import ExtCliProvider

_ENTRY_POINT_GROUP = "axiom.ext.cli.providers"


def _builtin_factories() -> dict[str, Callable[[], ExtCliProvider]]:
    """Map of verb -> factory for the Tier 1b built-in providers.

    Each factory is a zero-argument callable returning a fresh provider
    instance. Importing the command modules lazily keeps the CLI startup
    cost proportional to the verbs actually invoked.
    """
    from axiom.cli.ext.commands.completion import CompletionProvider
    from axiom.cli.ext.commands.config import ConfigProvider
    from axiom.cli.ext.commands.docs import DocsProvider
    from axiom.cli.ext.commands.doctor import DoctorProvider
    from axiom.cli.ext.commands.eval_verb import EvalProvider
    from axiom.cli.ext.commands.graph import GraphProvider
    from axiom.cli.ext.commands.init import InitProvider
    from axiom.cli.ext.commands.install import InstallProvider
    from axiom.cli.ext.commands.lint import LintProvider
    from axiom.cli.ext.commands.list import ListProvider
    from axiom.cli.ext.commands.migrate import MigrateProvider
    from axiom.cli.ext.commands.publish import PublishProvider
    from axiom.cli.ext.commands.quickstart import QuickstartProvider
    from axiom.cli.ext.commands.run import RunProvider
    from axiom.cli.ext.commands.scan import ScanProvider
    from axiom.cli.ext.commands.search import SearchProvider
    from axiom.cli.ext.commands.show import ShowProvider
    from axiom.cli.ext.commands.sign import SignProvider
    from axiom.cli.ext.commands.status import StatusProvider
    from axiom.cli.ext.commands.templates import TemplatesProvider
    from axiom.cli.ext.commands.test_verb import TestProvider
    from axiom.cli.ext.commands.uninstall import UninstallProvider
    from axiom.cli.ext.commands.update import UpdateProvider
    from axiom.cli.ext.commands.validate import ValidateProvider
    from axiom.cli.ext.commands.verify import VerifyProvider
    from axiom.cli.ext.commands.whoami import WhoamiProvider

    return {
        "init": InitProvider,
        "quickstart": QuickstartProvider,
        "templates": TemplatesProvider,
        "lint": LintProvider,
        "validate": ValidateProvider,
        "test": TestProvider,
        "doctor": DoctorProvider,
        "docs": DocsProvider,
        "config": ConfigProvider,
        "graph": GraphProvider,
        "run": RunProvider,
        "eval": EvalProvider,
        "migrate": MigrateProvider,
        "scan": ScanProvider,
        "sign": SignProvider,
        "verify": VerifyProvider,
        "publish": PublishProvider,
        "install": InstallProvider,
        "uninstall": UninstallProvider,
        "update": UpdateProvider,
        "list": ListProvider,
        "search": SearchProvider,
        "show": ShowProvider,
        "whoami": WhoamiProvider,
        "status": StatusProvider,
        "completion": CompletionProvider,
    }


def discover_providers() -> dict[str, ExtCliProvider]:
    """Return the registry of verb -> provider instance.

    Entry-point registrations win over built-ins when the verb collides.
    """
    providers: dict[str, ExtCliProvider] = {}

    # Built-ins first.
    for verb, factory in _builtin_factories().items():
        providers[verb] = factory()

    # Entry-point overrides.
    try:
        eps = _md.entry_points(group=_ENTRY_POINT_GROUP)
    except TypeError:  # pragma: no cover — pre-3.10 API; we require 3.11+
        eps = _md.entry_points().get(_ENTRY_POINT_GROUP, [])  # type: ignore[attr-defined]

    for ep in eps:
        try:
            obj = ep.load()
        except Exception:  # pragma: no cover — broken third-party install
            continue
        provider = obj() if isinstance(obj, type) else obj
        if not isinstance(provider, ExtCliProvider):
            continue
        providers[provider.verb] = provider

    return providers


__all__ = ["discover_providers"]
