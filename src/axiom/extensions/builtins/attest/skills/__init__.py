# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Attest skills (ADR-056). Read skills project to MCP; signing never does.

``attest.new`` and ``attest.sign`` are CLI-only: a person answers at the
prompt. Exposing them to an agent would let software sign (ADR-142).
``attest.propose`` is the one write on an agent surface: it puts a draft in a
person's tray and can neither sign it nor supply an observed value (unit L1).
``attest.supplement`` signs a supplement to a signed entry at the prompt (CLI only);
``attest.supplement_propose`` drafts one for a person (unit L2). ``attest.ack`` signs an
acknowledgement at the prompt (CLI only); ``attest.unacknowledged`` is a read (unit L3).
``attest.cosign`` signs a co-signature at the prompt (CLI only); ``attest.cosign_pending`` is a
read (unit L4).
"""

from __future__ import annotations

from axiom.infra.skill_declarations import Precondition
from axiom.infra.skills import SkillRegistry, SkillSpec

from . import (
    access,
    ack,
    admin,
    anchor,
    cosign,
    logbook,
    obligations,
    propose,
    read,
    sign,
    supplement,
    verify_all,
)

_READ = ("cli", "mcp", "agent_tool")

#: Reads of signed records are recorded on the action audit chain (unit X2). These keep
#: ``side_effects=False`` (the MCP annotation and the READ approval category are unchanged) and
#: are registered ``mutating=True`` so the registry writes their record.
AUDITED_READS = frozenset({"attest.show", "attest.verify", "attest.export"})

#: The entry an action acts on: named by its attestation id (``attest.show`` lists them).
_HAS_TARGET = Precondition(
    "target-named",
    "the signed entry to act on is named by its attestation_id",
    check=lambda params, ctx: bool(params.get("attestation_id")),
    fix="pass attestation_id, the id `attest show --logbook LOG` lists for the entry",
)
_PERSON_AT_PROMPT = Precondition(
    "person-at-prompt",
    "a person answers sign, hold or ask at an interactive prompt",
    check=lambda params, ctx: getattr(ctx, "user_prompt", None) is not None,
    fix="run it yourself at a terminal; software proposes with attest.supplement_propose",
)

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
        description=(
            "Show one signed record by id, or the latest records of a logbook at a site, "
            "narrowed by entry type, signer, time window, interval or a text in the content."
        ),
        inputs={
            "attestation_id": "str | None",
            "site": "str | None",
            "logbook": "str | None",
            "limit": "int",
            "type": "str | list[str] | None: entry type(s) to keep",
            "meaning": "str | list[str] | None",
            "signer": "str | None: a principal handle, @name:context",
            "since": "str | None: ISO 8601, inclusive, UTC when naive",
            "until": "str | None: ISO 8601, inclusive, UTC when naive",
            "interval": "str | None: kind:number, e.g. run:12",
            "text": "str | None: a case-insensitive text in the signed content",
        },
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="attest.intervals",
        fn=read.intervals_listing,
        description=(
            "List every interval a logbook has run at a site (when it opened and closed, how long, "
            "how many entries and missed checks fell inside it) and the gaps between intervals."
        ),
        inputs={
            "site": "str | None",
            "logbook": "str",
            "kind": "str | None: one declared interval kind",
        },
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
    ),
    SkillSpec(
        name="attest.verify",
        fn=read.verify,
        description=(
            "Verify a logbook's signed chain and name the first broken record, if any. "
            "Not the harness's own /verify, which checks a code change."
        ),
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
        name="attest.access",
        fn=access.access,
        description="Who read a log, when and through which surface: the access records on the action audit chain, newest first.",
        inputs={"logbook": "str | None", "since": "str | None", "limit": "int"},
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="attest.verify_all",
        fn=verify_all.verify_all,
        description="Verify every log's chain at every served site from sequence 1, record the result for the heartbeat check, and name the first break.",
        inputs={},
        surfaces=("cli",),
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
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
        name="attest.propose",
        fn=propose.propose,
        description=(
            "Propose a log entry for a person to complete and sign. Software only proposes: it "
            "cannot sign, and cannot fill a field the person must observe."
        ),
        inputs={
            "site": "str | None",
            "logbook": "str",
            "type": "str",
            "meaning": "str",
            "title": "str",
            "body": "str | None",
            "fields": "dict | list[str] | None",
            "for_principal": "str",
            "integration": "str | None",
        },
        surfaces=_READ,
        side_effects=True,
        idempotent=False,
        # It writes a draft for a person to complete and sign; it never signs.
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="attest.drafts",
        fn=propose.drafts,
        description="List the drafts waiting for you to sign, oldest first, with who proposed each.",
        inputs={
            "site": "str | None",
            "logbook": "str | None",
            "origin": "str | None",
            "limit": "int",
        },
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="attest.supplement",
        fn=supplement.supplement,
        description=(
            "Correct, clarify or add to a signed log entry, and sign the supplement at the prompt. "
            "The original never changes. CLI only; a person answers."
        ),
        inputs={
            "attestation_id": "str",
            "reason": "str",
            "title": "str | None",
            "body": "str | None",
            "fields": "list[str]",
            "site": "str | None",
        },
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
        # Signed records are never removed; a mistaken supplement is itself supplemented.
        risk="reversible-write",
        requires=(_HAS_TARGET, _PERSON_AT_PROMPT),
    ),
    SkillSpec(
        name="attest.supplement_propose",
        fn=propose.supplement_propose,
        description=(
            "Propose a correction, clarification or addition to a signed log entry, for the "
            "person who signed it (or another named person) to complete and sign. Software only "
            "proposes: it cannot sign, and cannot fill a field the person must observe."
        ),
        inputs={
            "attestation_id": "str",
            "reason": "str",
            "title": "str | None",
            "body": "str | None",
            "fields": "dict | list[str] | None",
            "for_principal": "str | None",
            "site": "str | None",
            "integration": "str | None",
        },
        surfaces=("mcp", "agent_tool"),
        side_effects=True,
        idempotent=False,
        # A draft: the person discards it or signs it.
        risk="reversible-write",
        requires=(_HAS_TARGET,),
    ),
    SkillSpec(
        name="attest.ack",
        fn=ack.ack,
        description=(
            "Acknowledge a signed log entry (I have seen this and it stands) and sign the "
            "acknowledgement at the prompt. CLI only; a person answers."
        ),
        inputs={"attestation_id": "str", "site": "str | None"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
        # Signed records are never removed; a second acknowledgement by the same person is refused.
        risk="reversible-write",
        requires=(_HAS_TARGET, _PERSON_AT_PROMPT),
    ),
    SkillSpec(
        name="attest.unacknowledged",
        fn=read.unacknowledged,
        description=(
            "List the entries of a log that nobody has acknowledged yet, or that a named person "
            "(or `me`) has not."
        ),
        inputs={
            "site": "str | None",
            "logbook": "str",
            "by": "str | None",
            "since_seq": "int | None",
        },
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="attest.cosign",
        fn=cosign.cosign,
        description=(
            "Co-sign a signed log entry as the second person its type requires, and sign the "
            "co-signature at the prompt. CLI only; a person answers."
        ),
        inputs={"attestation_id": "str", "site": "str | None"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
        # Signed records are never removed; one co-signature per entry.
        risk="reversible-write",
        requires=(_HAS_TARGET, _PERSON_AT_PROMPT),
    ),
    SkillSpec(
        name="attest.cosign_pending",
        fn=read.cosign_pending,
        description="List the entries of a log waiting for a co-signature, oldest first.",
        inputs={
            "site": "str | None",
            "logbook": "str",
            "for_roles": "list[str] | str | None",
            "older_than": "int | None",
        },
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
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
            audited = spec.side_effects is not False or spec.name in AUDITED_READS
            registry.register_skill(spec, mutating=audited)


def bind_default() -> SkillRegistry:
    registry = SkillRegistry()
    bind(registry)
    return registry


__all__ = ["AUDITED_READS", "SPECS", "bind", "bind_default"]
