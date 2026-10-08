# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Attest skills (ADR-056). Read skills project to MCP; signing never does.

``attest.new`` and ``attest.sign`` are CLI-only: a person answers at the
prompt. Exposing them to an agent would let software sign (ADR-142).
"""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, SkillSpec

from . import admin, anchor, logbook, obligations, read, sign

_READ = ("cli", "mcp", "agent_tool")

SPECS: tuple[SkillSpec, ...] = (
    SkillSpec(
        name="attest.logbook_list",
        fn=logbook.list_logbooks,
        description="List the logbooks declared on this node, their entry types and who may sign them.",
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
    ),
    SkillSpec(
        name="attest.logbook_validate",
        fn=logbook.validate,
        description="Validate a logbook declaration file without loading it.",
        inputs={"path": "Path"},
        surfaces=("cli",),
        side_effects=False,
        idempotent=True,
    ),
    SkillSpec(
        name="attest.show",
        fn=read.show,
        description="Show one signed record by id, or the latest records of a logbook at a site.",
        inputs={
            "attestation_id": "str | None",
            "site": "str | None",
            "logbook": "str | None",
            "limit": "int",
        },
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
    ),
    SkillSpec(
        name="attest.verify",
        fn=read.verify,
        description="Verify a logbook's signed chain and name the first broken record, if any.",
        inputs={"site": "str | None", "logbook": "str"},
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
    ),
    SkillSpec(
        name="attest.export",
        fn=read.export,
        description="Write an evidence package (records, public keys, standalone verify.py) for a logbook.",
        inputs={"site": "str | None", "logbook": "str", "out": "Path"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
    ),
    SkillSpec(
        name="attest.anchor",
        fn=anchor.run,
        description="Sign a Merkle root over every logbook head at a site (all sites with chains if none named).",
        inputs={"site": "str | None"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
    ),
    SkillSpec(
        name="attest.device_enroll",
        fn=admin.device_enroll,
        description="Enrol a signing device: class, location, fixed or portable (administration).",
        inputs={
            "site": "str | None",
            "device_id": "str",
            "device_class": "str",
            "location": "str | None",
            "mobility": "str",
        },
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
    ),
    SkillSpec(
        name="attest.device_reclaim",
        fn=admin.device_reclaim,
        description="Issue a fresh one-time claim code for an enrolled device (administration).",
        inputs={"device_id": "str"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
    ),
    SkillSpec(
        name="attest.device_retire",
        fn=admin.device_retire,
        description="Retire an enrolled device (administration).",
        inputs={"device_id": "str"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
    ),
    SkillSpec(
        name="attest.device_list",
        fn=admin.device_list,
        description="List the enrolled signing devices at a site.",
        inputs={"site": "str | None"},
        surfaces=("cli",),
        side_effects=False,
        idempotent=True,
    ),
    SkillSpec(
        name="attest.location_init",
        fn=admin.location_init,
        description="Create a location's secret in the vault for its rotating presence code.",
        inputs={"site": "str | None", "location": "str"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
    ),
    SkillSpec(
        name="attest.location_code",
        fn=admin.location_code,
        description="Show a location's current presence code (run on its fixed display).",
        inputs={"site": "str | None", "location": "str"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
    ),
    SkillSpec(
        name="attest.role_grant",
        fn=admin.role_grant,
        description="Give a person a signing role at a site (administration; every change is logged).",
        inputs={"site": "str | None", "principal": "str", "role": "str"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
    ),
    SkillSpec(
        name="attest.role_revoke",
        fn=admin.role_revoke,
        description="Take a signing role from a person at a site (administration; logged).",
        inputs={"site": "str | None", "principal": "str", "role": "str"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
    ),
    SkillSpec(
        name="attest.role_list",
        fn=admin.role_list,
        description="List who holds which signing roles at a site.",
        inputs={"site": "str | None"},
        surfaces=("cli",),
        side_effects=False,
        idempotent=True,
    ),
    SkillSpec(
        name="attest.obligations",
        fn=obligations.status,
        description="What is due in each open interval, when, and whether it is ok, due soon or missed.",
        inputs={"site": "str | None", "logbook": "str | None"},
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
    ),
    SkillSpec(
        name="attest.obligations_tick",
        fn=obligations.tick,
        description="Record, publish and notify obligation changes (run every minute by the schedule).",
        inputs={},
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
    ),
    SkillSpec(
        name="attest.new",
        fn=sign.new,
        description="Write an entry and sign it at the prompt. CLI only; a person answers.",
        inputs={
            "site": "str | None",
            "logbook": "str",
            "type": "str",
            "meaning": "str",
            "title": "str",
            "fields": "list[str]",
        },
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
    ),
    SkillSpec(
        name="attest.sign",
        fn=sign.sign,
        description="Complete and sign a draft proposed for you. CLI only; a person answers.",
        inputs={"draft_id": "str", "fields": "list[str]"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
    ),
)


def bind(registry: SkillRegistry) -> None:
    for spec in SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=spec.side_effects is not False)


def bind_default() -> SkillRegistry:
    registry = SkillRegistry()
    bind(registry)
    return registry


__all__ = ["SPECS", "bind", "bind_default"]
