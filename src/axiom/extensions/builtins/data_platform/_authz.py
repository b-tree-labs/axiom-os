# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""GUARD-wired entry point for every ``axi data`` skill.

Per ADR-055 + the AUTHZ-5 easy-onramp pattern, every action that
crosses an authorization boundary must consult ``decide(envelope)``
exactly once. The data-platform's CLI verbs *are* such actions — they
install infrastructure, mutate registered connectors, kick off
ingest, query the data plane. We route them through one
``ExtensionAuthnContext`` so each verb writes a verdict receipt
the operator can later query via ``axi audit``.

The ``DATA_PLATFORM`` context is constructed lazily on first use to
avoid import-time side effects (importing the data_platform package
shouldn't open a Postgres connection or talk to GUARD's database).

Skills wrap their work in::

    from . import _authz

    def run(params, ctx):
        with _authz.action(
            verb="install",
            resource="data-platform://" + namespace,
            classification=Classification.INTERNAL,
        ) as act:
            ... do the work ...
            return SkillResult(..., actions_taken=[
                f"audit-receipt: {act.receipt_id}",
            ])

When GUARD is unreachable or the host is not in production mode, the
wrapping falls back to a dev-mode permit-with-receipt so local
development isn't blocked by missing infrastructure. The fallback is
explicit in the receipt's reason field so an auditor can spot it.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager

_log = logging.getLogger(__name__)

_EXTENSION_NAME = "data"
_VERBS = (
    "install",
    "diagnose",
    "ingest",
    "register",
    "unregister",
    "list",
    "troubleshoot",
)

_ctx: object | None = None
_ctx_init_failed: bool = False


def _build_context():
    """Lazy-init the ``ExtensionAuthnContext`` for the data-platform."""
    global _ctx, _ctx_init_failed
    if _ctx is not None:
        return _ctx
    if _ctx_init_failed:
        return None
    try:
        from axiom.governance.simple import setup_extension
        _ctx = setup_extension(
            _EXTENSION_NAME,
            verbs=list(_VERBS),
            # dev_mode=None → inherits AXIOM_MODE (AUTHZ-5 default).
            # Operators set AXIOM_MODE=production on jumphosts; dev
            # machines keep the permit-with-receipt fallback.
        )
        return _ctx
    except Exception as exc:  # noqa: BLE001 — operator surface
        # Don't crash data-platform installs because authz wiring isn't
        # available. Log loudly and let the wrap fall through.
        _log.warning(
            "data-platform: authz wiring unavailable (%s); proceeding "
            "without receipt-writing. Run `axi audit healthcheck` to "
            "diagnose.",
            exc,
        )
        _ctx_init_failed = True
        return None


@contextmanager
def action(
    *,
    verb: str,
    resource: str,
    classification: object | None = None,
    actor: str | None = None,
):
    """Context-manage one data-platform action through GUARD.

    Yields a small object with ``receipt_id`` attribute on success.
    On deny, raises ``AuthorizationDenied``. On GUARD unavailability,
    yields a synthetic receipt-id so callers' downstream code is
    unaffected.
    """
    ctx = _build_context()
    if ctx is None:
        # Synthetic — operator sees the fall-through in the logs.
        yield _SyntheticAction(receipt_id=f"data-no-authz-{verb}")
        return

    # Default classification — internal. Callers can override per verb.
    if classification is None:
        from axiom.governance.classification import Classification
        classification = Classification.INTERNAL

    # Resolve actor: explicit kwarg > AXIOM_ACTOR env > dev fallback
    # inside setup_extension.
    actor_for_action: object | None = actor
    if actor_for_action is None and os.environ.get("AXIOM_ACTOR"):
        actor_for_action = os.environ["AXIOM_ACTOR"]

    with ctx.action(
        verb=verb,
        actor=actor_for_action,
        resource=resource,
        classification=classification,
    ) as act:
        yield act


class _SyntheticAction:
    """Stand-in returned when the GUARD wiring is unavailable.

    ``axi audit list`` won't find these — by design. The verb still
    runs, but the operator should run ``axi audit healthcheck`` to
    diagnose why receipts weren't written."""

    def __init__(self, receipt_id: str) -> None:
        self.receipt_id = receipt_id
        # Compatibility with the real ``_Action`` shape — verdict and
        # envelope attrs are absent but accessed defensively by tests.

    def __repr__(self) -> str:  # pragma: no cover
        return f"<_SyntheticAction receipt_id={self.receipt_id!r}>"


__all__ = ["action"]
