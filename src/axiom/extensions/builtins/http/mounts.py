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
    """``/ingest`` — data-platform push ingest (SRV-005), both lanes.

    One namespace claim carrying two lanes (ADR-106; spec-signal-ingest §5.1):
    ``POST /ingest`` for documents and ``POST /ingest/rows`` for tabular
    batches. Each resolves a connector-specific sink per request from the
    connector registry (the single source of bronze root + rules + store), so
    a push over HTTP lands in the same bronze tree under the same provenance
    rules as the pull/CDC/scheduled paths. An unknown ``source`` fails loudly
    with 422 instead of silently quarantining into a rule-less tree (the prior
    bug). Site and tier ceiling come from the credential (spec §5.2).
    """
    from fastapi import APIRouter

    from axiom.extensions.builtins.data_platform.ingest_sink import (
        make_connector_sink_resolver,
        make_connector_tabular_sink_resolver,
    )
    from axiom.extensions.builtins.data_platform.ingest_sink.api import (
        build_ingest_router,
        build_tabular_ingest_router,
    )
    from axiom.extensions.builtins.data_platform.ingest_sink.edge import EdgeOutbox
    from axiom.extensions.builtins.data_platform.ingest_sink.summary import (
        build_ingest_summary_router,
    )

    # One FLAT router: the lanes' routes are appended directly rather than
    # nested through include_router. FastAPI >= 0.138 includes routers lazily
    # (an ``_IncludedRouter`` placeholder in ``.routes``), which the composed
    # app's route table — built from ``spec.router.routes`` — cannot see, so a
    # nested lane would 404 at the middleware before it ever reached FastAPI.
    router = APIRouter()
    # store=AUTO_STORE → embed enabled on the document lane
    # The read peer of /ingest/rows ("did my rows land?"). On an edge it counts
    # the bronze this node holds; elsewhere the published tier.
    if EdgeOutbox.from_env() is not None:
        from axiom.extensions.builtins.data_platform.ingest_sink import edge_summary

        summary = build_ingest_summary_router(
            summarize=edge_summary.summarize, count_channels=edge_summary.count_channels
        )
    else:
        summary = build_ingest_summary_router()
    for lane in (
        build_ingest_router(sink_resolver=make_connector_sink_resolver()),
        build_tabular_ingest_router(sink_resolver=make_connector_tabular_sink_resolver()),
        summary,
    ):
        router.routes.extend(lane.routes)
    return MountSpec(
        prefix="/ingest",
        router=router,
        extension="data_platform",
        bind="127.0.0.1",
        trust_zone="loopback",
        functions=("ingest",),
    )


def edge_export_mount_spec() -> MountSpec | None:
    """``/edge`` — the ingest edge's export, pulled by the owning node (ADR-177).

    Present only where an outbox is configured (``AXIOM_INGEST_OUTBOX_DIR``),
    so no node that is not an edge exposes an export it has nothing to put in.
    Its own mount name, ``edge_export``, so the scope that reads it
    (``edge_export:read``) is granted on purpose and to nothing else; the
    handlers also refuse any credential bound to a site.
    """
    from axiom.extensions.builtins.data_platform.ingest_sink.edge import (
        EdgeOutbox,
        build_edge_router,
    )

    outbox = EdgeOutbox.from_env()
    if outbox is None:
        return None
    return MountSpec(
        prefix="/edge",
        router=build_edge_router(outbox),
        extension="edge_export",
        functions=("ingest",),
    )


def maintenance_mount_spec() -> MountSpec | None:
    """``/maintenance`` — the edge's maintenance mailbox (ADR-183).

    Present only on an edge (an outbox is configured). Its own mount name, so
    ``maintenance:read`` / ``maintenance:invoke`` are granted on purpose: to a
    site's node for its own requests and results, and to listed operators.
    """
    from axiom.extensions.builtins.data_platform.ingest_sink.maintenance_box import (
        MaintenanceBox,
        build_maintenance_router,
    )

    box = MaintenanceBox.from_env()
    if box is None:
        return None
    return MountSpec(
        prefix="/maintenance",
        router=build_maintenance_router(box),
        extension="maintenance",
        functions=("ingest",),
    )


def approval_link_mount_spec() -> MountSpec | None:
    """``/approve`` — where a person's signed one-time approval link lands (ADR-179 §4).

    Public, because the link itself is the credential, and it can do nothing on
    its own: it only files the link for the site's node, which verifies it.
    Present only where the maintenance mailbox is.
    """
    from axiom.extensions.builtins.data_platform.ingest_sink.maintenance_box import (
        MaintenanceBox,
        build_approval_router,
    )

    box = MaintenanceBox.from_env()
    if box is None:
        return None
    return MountSpec(
        prefix="/approve",
        router=build_approval_router(box),
        extension="maintenance",
        requires_authz=False,
        functions=("ingest",),
    )


def release_channel_mount_spec() -> MountSpec | None:
    """``/releases`` — signed release-channel manifests nodes poll (ADR-179 §3). Public: a node
    trusts the signature, not the host. Present only where a channel directory is configured."""
    import os
    from pathlib import Path

    from axiom.extensions.builtins.update.channel import DIR_ENV, build_channel_router

    directory = os.environ.get(DIR_ENV)
    if not directory:
        return None
    return MountSpec(
        prefix="/releases",
        router=build_channel_router(Path(directory)),
        extension="update",
        requires_authz=False,
        functions=("ingest",),
    )


def health_mount_spec() -> MountSpec:
    """``/healthz`` — liveness for a load balancer or a monitor. Public, and
    says only that the process is up."""
    from axiom.extensions.builtins.data_platform.ingest_sink.edge import (
        build_health_router,
    )

    return MountSpec(
        prefix="/healthz",
        router=build_health_router(),
        extension="http",
        requires_authz=False,
        functions=("health",),
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
#
# `agent_mount.agent_mount_spec` is deliberately absent. It serves an agent
# tool-use loop, which the 2026-06-29 serving-path decision allows only as a
# bounded opt-in, so a deployment registers it by hand and names the turn
# deadline while doing so. Adding it here would turn it on for every node.
BUILTIN_MOUNT_FACTORIES = (
    ingest_mount_spec,
    edge_export_mount_spec,
    maintenance_mount_spec,
    approval_link_mount_spec,
    release_channel_mount_spec,
    health_mount_spec,
    classroom_mount_spec,
    herald_mount_spec,
    chat_mount_spec,
)


__all__ = [
    "BUILTIN_MOUNT_FACTORIES",
    "approval_link_mount_spec",
    "release_channel_mount_spec",
    "chat_mount_spec",
    "classroom_mount_spec",
    "edge_export_mount_spec",
    "health_mount_spec",
    "herald_mount_spec",
    "ingest_mount_spec",
    "maintenance_mount_spec",
]
