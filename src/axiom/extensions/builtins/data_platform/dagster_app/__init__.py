# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Dagster orchestration shim for the data platform.

This package is a *thin* wrapper around the pure-Python orchestration in
``data_platform/orchestration/``. Dagster owns scheduling + run +
materialization tracking; the actual logic of one source → bronze → RAG
pass lives in :func:`run_source_to_rag` and is exercised by unit tests
without Dagster.

Importing this package requires the ``[data-platform]`` optional extra
(``pip install "axiom-os-lm[data-platform]"``) — Dagster, dbt-core,
duckdb, pyiceberg, etc. The import is guarded so a clear message
surfaces when those are missing.
"""

from __future__ import annotations


def _require_dagster() -> None:
    try:
        import dagster  # noqa: F401
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RuntimeError(
            "Dagster is not installed. Install the data-platform extra:\n"
            '  pip install "axiom-os-lm[data-platform]"\n'
            "This pulls in dagster, dbt-core, duckdb, and pyiceberg."
        ) from exc


def load_definitions():
    """Return the Dagster ``Definitions`` object for the platform.

    The Dagster CLI loads this via the ``-m`` flag:
    ``dagster dev -m axiom.extensions.builtins.data_platform.dagster_app:load_definitions``

    We call ``_build_definitions()`` directly rather than going through
    the ``definitions`` module attribute. The attribute was meant to
    be ``__getattr__``-mediated for lazy import, but a module-level
    ``definitions = None`` shadowed the lookup — ``from .defs import
    definitions`` always returned ``None`` and Dagster rejected it with
    ``DagsterInvariantViolationError: Loadable attributes must be ...
    Got None.``
    """
    _require_dagster()
    from .defs import _build_definitions

    return _build_definitions()


__all__ = ["load_definitions"]
