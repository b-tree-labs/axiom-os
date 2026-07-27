# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""MountSpec factories for the built-in HTTP consumers (spec-serve §4.1).

Each factory builds the consumer's existing ``APIRouter`` (no route
handler changes — SRV-005) and wraps it in a :class:`MountSpec` with a
sensible default backend. A deployment that needs a non-default backend
constructs the router itself and registers its own ``MountSpec``; these
factories are the zero-config path so installing a consumer is enough to
mount it.

The three current consumers (ingest_sink, classroom, herald) already
decorate their routes with their full path (``/ingest``,
``/classroom/…``, ``/herald/inbound/…``), so the routers are included
without an extra prefix; ``MountSpec.prefix`` is the namespace claim used
for the route table + conflict detection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.paths import get_user_state_dir

from .registry import MountSpec


def ingest_mount_spec() -> MountSpec:
    """``/ingest`` — data-platform push ingest (SRV-005).

    Resolves a connector-specific :class:`IngestSink` per request from the
    connector registry (the single source of bronze root + rules + store), so an
    item pushed over HTTP lands in the same bronze tree under the same provenance
    rules as the pull/CDC/Dagster paths. An unknown ``source`` fails loudly with
    422 instead of silently quarantining into a rule-less tree (the prior bug).
    """
    from axiom.extensions.builtins.data_platform.ingest_sink import (
        make_connector_sink_resolver,
    )
    from axiom.extensions.builtins.data_platform.ingest_sink.api import (
        build_ingest_router,
    )

    resolver = make_connector_sink_resolver()  # store=AUTO_STORE → embed enabled
    return MountSpec(
        prefix="/ingest",
        router=build_ingest_router(sink_resolver=resolver),
        extension="data_platform",
        bind="127.0.0.1",
        trust_zone="loopback",
    )


def herald_mount_spec() -> MountSpec:
    """``/herald/inbound`` — notifications inbound gateway (SRV-005)."""
    from axiom.extensions.builtins.notifications.gateway.routes import (
        build_gateway_router,
    )
    from axiom.extensions.builtins.notifications.gateway.verify import (
        VerifierRegistry,
    )

    class _NullBus:
        """No-op bus default — a deployment injects the live bus.

        Keeps the route mountable so ``axi serve --list`` shows it even on
        a node that hasn't wired its message bus yet.
        """

        def publish(
            self,
            subject: str,
            payload: dict[str, Any] | None = None,
            source: str = "",
        ) -> None:
            return None

    return MountSpec(
        prefix="/herald/inbound",
        router=build_gateway_router(
            bus=_NullBus(), verifiers=VerifierRegistry()
        ),
        extension="notifications",
        # Inbound webhooks arrive over the LAN/public edge, not loopback.
        bind="0.0.0.0",
        trust_zone="lan",
    )


def classroom_mount_spec() -> MountSpec:
    """``/classroom`` — classroom coordinator API (SRV-005)."""
    from axiom.extensions.builtins.classroom.classroom_api import (
        _build_core_router,
    )
    from axiom.extensions.builtins.classroom.coordinator_cohort_store import (
        FileCohortStore,
    )
    from axiom.extensions.builtins.classroom.coordinator_invite_registry import (
        FileInviteRegistry,
    )
    from axiom.vega.federation.identity import generate_identity

    state = Path(get_user_state_dir()) / "classroom"
    state.mkdir(parents=True, exist_ok=True)
    identity = generate_identity(
        owner="@coordinator:local", keys_dir=state / "keys"
    )
    router = _build_core_router(
        coordinator_identity=identity,
        classroom_id="default",
        cohort_store=FileCohortStore(base_dir=state),
        invite_registry=FileInviteRegistry(path=state / "invites.json"),
        on_student_joined=None,
        interaction_store=None,
    )
    return MountSpec(
        prefix="/classroom",
        router=router,
        extension="classroom",
        bind="127.0.0.1",
        trust_zone="loopback",
    )


def chat_mount_spec() -> MountSpec:
    """Gateway — the public serving contract (spec-serve §14).

    Exposes the OpenAI-compatible chat API at the top level
    (``/v1/chat/completions``, ``/v1/models``, ``/v1/info``) plus the
    legacy chat surface (``/chat``, ``/health``, ``/context``, ``/reset``,
    federation search) by building the router with ``prefix=""``. This is
    the contract the live deployment serves, so it is the gateway.

    Authz: the gateway is now gated through the uniform authz seam
    (``requires_authz=True``, RATIONALIZE-3) like every other mount — the
    bearer token is resolved to a principal and run through GUARD by the
    auto-wired :func:`authz_hook.maybe_default_authz_hook` adapter (legacy
    ``AXIOM_API_KEY`` is honored as a token bound to ``@api:local`` during the
    migration). No more per-mount ``requires_authz=False`` opt-out onto a
    hand-rolled bearer check.
    """
    from .chat_server import build_chat_router

    return MountSpec(
        prefix="/v1",
        router=build_chat_router(prefix=""),
        extension="gateway",
        bind="127.0.0.1",
        trust_zone="loopback",
    )


# The built-in consumers `serve` mounts when no manifest discovery has
# supplied them (SRV-005). Ordered for readability; compose sorts by prefix.
BUILTIN_MOUNT_FACTORIES = (
    ingest_mount_spec,
    classroom_mount_spec,
    herald_mount_spec,
    chat_mount_spec,
)


__all__ = [
    "BUILTIN_MOUNT_FACTORIES",
    "chat_mount_spec",
    "classroom_mount_spec",
    "herald_mount_spec",
    "ingest_mount_spec",
]
