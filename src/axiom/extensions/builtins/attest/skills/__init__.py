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
read (unit L4). ``attest.seal`` closes out a period at the prompt (CLI only);
``attest.seal_propose`` drafts one with a proposed summary; ``attest.seal_preview`` is a read
(unit L5). ``attest.export`` writes an evidence package (CLI only); ``attest.export_preview`` is a
read (unit L6). ``attest.import`` dry-runs a file of history and, with the dry run's digest, imports
it at the prompt (CLI only); ``attest.import_withdraw`` withdraws a batch at the prompt (CLI only);
``attest.import_preview`` is the dry run an agent may ask for, which never writes (#1804).
"""

from __future__ import annotations

import functools
from dataclasses import replace

from axiom.infra.skill_declarations import Precondition
from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from . import (
    access,
    ack,
    admin,
    anchor,
    archive,
    blobs,
    cosign,
    crosschecks,
    imports,
    logbook,
    obligations,
    propose,
    read,
    reference,
    retention,
    scale_seed,
    seal,
    search,
    sign,
    supplement,
    tier,
    tombstone,
    verify_all,
)

_READ = ("cli", "mcp", "agent_tool")

#: Reads of signed records are recorded on the action audit chain (unit X2). These keep
#: ``side_effects=False`` (the MCP annotation and the READ approval category are unchanged) and
#: are registered ``mutating=True`` so the registry writes their record.
AUDITED_READS = frozenset({"attest.show", "attest.verify", "attest.export", "attest.filters", "attest.blobs"})

#: The entry an action acts on: named by its attestation id (``attest.show`` lists them).
_HAS_TARGET = Precondition(
    "target-named",
    "the signed entry to act on is named by its attestation_id",
    check=lambda params, ctx: bool(params.get("attestation_id")),
    fix="pass attestation_id, the id `log LOG show` lists for the entry",
)
_PERSON_AT_PROMPT = Precondition(
    "person-at-prompt",
    "a person answers sign, hold or ask at an interactive prompt",
    check=lambda params, ctx: getattr(ctx, "user_prompt", None) is not None,
    fix="run it yourself at a terminal; software proposes with attest.supplement_propose",
)

#: The archived period to rehydrate: named by its archive id (``attest.archives`` lists them).
_ARCHIVE_NAMED = Precondition(
    "archive-named",
    "the archived period is named by its archive_id",
    check=lambda params, ctx: bool(params.get("archive_id")),
    fix="pass archive_id, as `attest.archives` lists it or a search result's period names it",
)

SPECS: tuple[SkillSpec, ...] = (
    SkillSpec(
        name="attest.logbook_list",
        fn=logbook.list_logbooks,
        description=(
            "List the logs declared on this node, their entry types and who may sign them; "
            "with `logbook` (an id or a handle), that one log."
        ),
        inputs={"logbook": "str | None", "site": "str | None"},
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
    ),
    SkillSpec(
        name="attest.logbook_validate",
        fn=logbook.validate,
        description="Validate a log declaration file without loading it.",
        inputs={"path": "Path"},
        surfaces=("cli",),
        side_effects=False,
        idempotent=True,
    ),
    SkillSpec(
        name="attest.show",
        fn=read.show,
        description=(
            "Show one signed record by id, or the latest records of a log at a site, "
            "narrowed by entry type, signer, time window, interval, a text in the content, or the "
            "log's own declared field filters (where)."
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
            "where": "list[{field, op, value, unit?}] | None: the log's declared filters "
                     "(attest.filters lists them), op eq, contains, gte or lte; the same as "
                     "field.<id>, field.<id>.contains, .gte, .lte and .unit keys",
        },
        values={"where": "log_filter"},
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="attest.filters",
        fn=read.filters_listing,
        description=(
            "List the fields a log declares as filters (label, control, unit and accepted units, "
            "which entry types) and, for a choice or an exact-value text, the values you may pick, "
            "from entries at your site only; pass them to attest.show as field.<id> or where."
        ),
        inputs={"site": "str | None", "logbook": "str", "field": "str | None: one filter",
                "prefix": "str | None: values starting with this"},  # fmt: skip
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="attest.reindex",
        fn=read.reindex,
        description=(
            "Rebuild a log's field index at a site from its signed records (administration). "
            "Never touches a signed row; filters give the same answers after."
        ),
        inputs={"site": "str | None", "logbook": "str"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=True,
        # The field index is a projection rebuilt from the signed chain; no signed row changes.
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="attest.intervals",
        fn=read.intervals_listing,
        description=(
            "List every interval a log has run at a site (when it opened and closed, how long, "
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
            "Verify a log's signed chain and name the first broken record, if any. "
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
        description=(
            "Write an evidence package for a log: with no format, the records, public keys and "
            "standalone verify.py; with formats and filters (unit L6), the whole chain, the "
            "filtered views (JSON Lines, text, CSV, declared forms), keys, verify.py and the "
            "verification report, stored and recorded as an export."
        ),
        inputs={"site": "str | None", "logbook": "str", "out": "Path | None",
                "format": "list[str] | str | None", "form": "list[str] | str | None",
                "entry_type": "list[str] | None", "meaning": "list[str] | None",
                "signer": "str | None", "from": "str | None", "to": "str | None",
                "interval": "str | None", "q": "str | None",
                "where": "list[{field, op, value, unit?}] | None",
                "include_supplements": "bool"},  # fmt: skip
        values={"where": "log_filter"},
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
        name="attest.blobs",
        fn=blobs.list_blobs,
        description=(
            "List the attachment blobs stored for a site, by hash and size (administration). "
            "Never their bytes or a location; the listing is recorded as a read."
        ),
        inputs={"site": "str | None"},
        surfaces=("cli",),
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="attest.blobs_check",
        fn=blobs.check,
        description=(
            "Restore validation for attachments: every hash a signed record names resolves to a "
            "blob of that hash, and every stored blob is named by a record; names each record "
            "that fails."
        ),
        inputs={"site": "str | None"},
        surfaces=("cli",),
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
        name="attest.scale_seed",
        fn=scale_seed.run,
        description=(
            "Dev only: write N fictional entries across twenty fixture logs and matching Timeline "
            "rows into a lane's own database, deterministic from a seed (the LS-1 scale fixture)."
        ),
        inputs={"scale": "int", "seed": "int | None", "site": "str | None",
                "no_journal": "bool | None", "declare": "bool | None"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="attest.anchor",
        fn=anchor.run,
        description="Sign a Merkle root over every log head at a site (all sites with chains if none named).",
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
        description=(
            "Give a person a signing role at a site, or the importer role for one log with an "
            "expiry (--log, --until) (administration; every change is logged)."
        ),
        inputs={"site": "str | None", "principal": "str", "role": "str",
                "logbook": "str | None", "until": "str | None"},  # fmt: skip
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
    ),
    SkillSpec(
        name="attest.role_revoke",
        fn=admin.role_revoke,
        description="Take a signing role, or a log's importer role, from a person (administration; logged).",
        inputs={"site": "str | None", "principal": "str", "role": "str", "logbook": "str | None"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
    ),
    SkillSpec(
        name="attest.role_list",
        fn=admin.role_list,
        description="List who holds which signing roles at a site, and who may import into which log until when.",
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
        name="attest.crosschecks",
        fn=crosschecks.crosschecks,
        description=(
            "A log's cross-check facts (each signed value compared after signing with an independent "
            "source: agrees, disagrees or not checked, and why) and each predicate's state at your "
            "site (consistent, a discrepancy, or a source that cannot say). Facts beside the records, "
            "never in them."
        ),
        inputs={"site": "str | None", "logbook": "str",
                "result": "str | None: agrees, disagrees or not_checked",
                "attestation_id": "str | None: one record's facts"},  # fmt: skip
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
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
            "occurred_at": "str | None",
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
        name="attest.seal",
        fn=seal.seal,
        description=(
            "Close out a period of a log: sign a summary, the open items the server found and "
            "the chain head, and anchor the site. CLI only; a person answers."
        ),
        inputs={
            "site": "str | None",
            "logbook": "str",
            "boundary": "str | None",
            "summary": "str",
            "from_seq": "int | None",
            "to_seq": "int | None",
        },
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
        # A seal is a signed record and locks nothing; a mistaken one is supplemented.
        risk="reversible-write",
        requires=(_PERSON_AT_PROMPT,),
    ),
    SkillSpec(
        name="attest.seal_propose",
        fn=propose.seal_propose,
        description=(
            "Propose a close-out of a log's period with a summary, for a person who may seal it to "
            "read and sign. Software only proposes: the server computes what the seal covers, and "
            "nothing is sealed until a person signs."
        ),
        inputs={
            "site": "str | None",
            "logbook": "str",
            "boundary": "str | None",
            "summary": "str",
            "for_principal": "str",
            "from_seq": "int | None",
            "to_seq": "int | None",
            "integration": "str | None",
        },
        surfaces=("mcp", "agent_tool"),
        side_effects=True,
        idempotent=False,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="attest.seal_due",
        fn=read.seal_due,
        description=(
            "List a log's clock periods that ended and are not closed, with the action that closes "
            "each; only for a person who may close the log. Nothing is written."
        ),
        inputs={"site": "str | None", "logbook": "str"},
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="attest.seal_preview",
        fn=read.seal_preview,
        description=(
            "Show what a close-out of a log's period would state: the covered range, the chain "
            "head and the open items. Nothing is written."
        ),
        inputs={
            "site": "str | None",
            "logbook": "str",
            "boundary": "str | None",
            "from_seq": "int | None",
            "to_seq": "int | None",
        },
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="attest.export_preview",
        fn=read.export_preview,
        description=(
            "Preview an export of a log: at most 200 of the selected records rendered as text, "
            "CSV or JSON Lines, with the same filters as an export. Writes nothing."
        ),
        inputs={"site": "str | None", "logbook": "str", "format": "str",
                "entry_type": "list[str] | None", "meaning": "list[str] | None",
                "signer": "str | None", "from": "str | None", "to": "str | None",
                "interval": "str | None", "q": "str | None",
                "where": "list[{field, op, value, unit?}] | None",
                "include_supplements": "bool"},  # fmt: skip
        values={"where": "log_filter"},
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="attest.sealed_and_packaged",
        fn=read.sealed_and_packaged,
        description=(
            "List what was sealed and packaged: each log close-out's anchor and each evidence "
            "package, newest first, with when, by whom and a link; the digest one step away."
        ),
        inputs={"limit": "int | None"},
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="attest.export_blank",
        fn=read.export_blank,
        description=(
            "Print a log's declared form blank, with no entries and page numbers, for a backup "
            "binder. Reads no entry; writes nothing."
        ),
        inputs={"site": "str | None", "logbook": "str", "form": "str", "out": "str | None"},
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="attest.sign",
        fn=sign.sign,
        description=(
            "Complete and sign a draft proposed for you. CLI only; a person answers. With "
            "`logbook`, a draft of another log is refused."
        ),
        inputs={"draft_id": "str", "fields": "list[str]", "logbook": "str | None"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
    ),
    SkillSpec(
        name="attest.import",
        fn=imports.run,
        description=(
            "Import history from another system onto a log's chain as records imported, not "
            "signed. A dry run by default (counts, every mapping decision, warnings, the input's "
            "digest); with `commit` = that digest, writes at the prompt for a person holding the "
            "importer role for the log. CLI only."
        ),
        inputs={"site": "str | None", "logbook": "str", "path": "str", "commit": "str | None",
                "source": "str | None"},  # fmt: skip
        surfaces=("cli",),
        side_effects=True,
        idempotent=True,
        # Never removed; a mistaken batch is withdrawn by a record and imported again.
        risk="reversible-write",
        # A dry run needs nobody; the commit asks the person at the prompt itself.
        requires=(),
    ),
    SkillSpec(
        name="attest.import_preview",
        fn=imports.preview,
        description=(
            "Dry-run an import of mapped history (JSON Lines text) into a log: the counts, every "
            "mapping decision, the warnings and the digest a person commits. Writes nothing; "
            "only a person commits an import."
        ),
        inputs={"site": "str | None", "logbook": "str", "content": "str", "source": "str | None"},
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="attest.import_withdraw",
        fn=imports.withdraw,
        description=(
            "Withdraw a whole import batch from a log with a reason, at the prompt: one withdrawal "
            "record, nothing deleted; the history can then be imported again. CLI only."
        ),
        inputs={"site": "str | None", "logbook": "str", "batch": "str", "reason": "str"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
        risk="reversible-write",
        requires=(_PERSON_AT_PROMPT,),
    ),
    SkillSpec(
        name="attest.imports",
        fn=imports.list_batches,
        description=(
            "List a log's import batches: when, by whom, how many entries, and any withdrawal. "
            "Not the harness's own /import, which imports harness settings."
        ),
        inputs={"site": "str | None", "logbook": "str"},
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="attest.archives",
        fn=archive.listing,
        description=(
            "List a log's sealed periods archived as packages: the range, the package digest, "
            "whether a stored verify report holds for it, and whether it was retired. Metadata only."
        ),
        inputs={"site": "str | None", "logbook": "str"},
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="attest.rehydrate",
        fn=archive.rehydrate,
        description=(
            "Read an archived period (archive_id, from attest.archives or a search result's period) "
            "again from its verified package, as chained, through the log's door: the read is "
            "recorded with the archive as its source, and a retired period's records are searchable "
            "in full again until the node's warm window ends. A removed record answers as removed."
        ),
        inputs={"site": "str | None", "logbook": "str", "archive_id": "str"},
        # It writes: the period's records go back into the index as warm rows (expiring, derived,
        # rebuildable). A write is not offered on MCP (software may not write through it), and every
        # tool the chat offers must also be an MCP tool (MVP-02-A5), so it is a terminal verb only.
        surfaces=("cli",),
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(_ARCHIVE_NAMED,),
    ),
    SkillSpec(
        name="attest.archive",
        fn=archive.archive,
        description=(
            "Archive a log's latest sealed period (or seal_id): write the verifiable package to the "
            "archive store, verify it offline and switch the period's reads to it. Nothing signed "
            "changes; the hot copy stays until a person retires it."
        ),
        inputs={"site": "str | None", "logbook": "str", "seal_id": "str | None",
                "target": "str | None: a named archive target"},  # fmt: skip
        surfaces=("cli",),
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="attest.archive_retire",
        fn=archive.retire,
        description=(
            "Retire the hot copy of an archived period, by a person, naming the package digest: "
            "refused without a stored verify report for exactly that package and a change record."
        ),
        inputs={"archive_id": "str", "package_digest": "str",
                "override_soak": "str | None: why the soak may be cut short"},  # fmt: skip
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
        # What retire drops (fine vectors) is derived from gold; the package and the chain stay.
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="attest.tier_move",
        fn=tier.move,
        description=(
            "Move one log's calendar year to the cold storage tier through the switch runner: "
            "refused inside the hot window or without a verified archive of every period in it. "
            "Every read is unchanged; rollback moves it back."
        ),
        inputs={"site": "str | None", "logbook": "str", "year": "int: the calendar year",
                "rollback": "bool | None: move a committed year back",
                "reason": "str | None: why, for a rollback"},  # fmt: skip
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
        # The rows move between two tables of one database and back; nothing is removed.
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="attest.retention",
        fn=retention.report,
        description=(
            "Show a log's retention: as declared, as in force at the site (its class's floor and any "
            "lengthening), each sealed period's archive and tombstone state, and what falls due next."
        ),
        inputs={"site": "str | None", "logbook": "str"},
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="attest.tombstone_preview",
        fn=retention.preview,
        description=(
            "Dry run of a tombstone of one archived period: every record and digest, attachment and "
            "archived package it would remove under the log's retention rule. Removes nothing."
        ),
        inputs={"site": "str | None", "logbook": "str", "archive_id": "str | None"},
        surfaces=("cli",),
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="attest.tombstone",
        fn=tombstone.tombstone,
        description=(
            "Remove the content of a sealed, archived period under the log's retention rule: sign a "
            "tombstone naming the period, the rule and everything that goes. CLI only; a person answers."
        ),
        inputs={"site": "str | None", "logbook": "str", "archive_id": "str"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
        # A tombstone is a signed record; nothing is removed until its switch is retired.
        risk="reversible-write",
        requires=(_PERSON_AT_PROMPT,),
    ),
    SkillSpec(
        name="attest.tombstone_propose",
        fn=retention.propose,
        description=(
            "Propose a tombstone of an archived period for a person who may sign one. Software only "
            "proposes: the server computes what would go, and nothing is removed until a person signs "
            "and a named approver retires the switch."
        ),
        inputs={"site": "str | None", "logbook": "str", "archive_id": "str", "for_principal": "str",
                "integration": "str | None"},  # fmt: skip
        surfaces=("mcp", "agent_tool"),
        side_effects=True,
        idempotent=False,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="attest.retention_commit",
        fn=retention.commit,
        description=(
            "Commit a signed tombstone's switch: the rule and the chain are checked again and reads of "
            "the period show no content. Reversible; nothing is removed yet."
        ),
        inputs={"tombstone_id": "str"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="attest.retention_erase",
        fn=retention.erase,
        description=(
            "Remove the content a committed tombstone names, as its named approver: the deletion is "
            "recorded first, then the records' content, blobs, index rows, fragments and the archived "
            "package go. Refused under a hold or when the rule no longer allows it."
        ),
        inputs={"tombstone_id": "str", "override_soak": "str | None: why the soak may be cut short"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
        risk="destructive",
        requires=(),
    ),
    SkillSpec(
        name="attest.seal_schedule",
        fn=archive.schedule,
        description=(
            "Put each log's sealing on the schedule at its declared cadence (a clock period's end "
            "plus its grace); a log with no cadence is sealed by hand. Idempotent."
        ),
        inputs={"site": "str | None", "logbook": "str | None"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="attest.reference",
        fn=reference.run,
        description=(
            "Follow a memory reference to its signed record: the record as of the newest record "
            "gold holds for its log, with a link to it, if you may read it now. A record whose "
            "content was removed under its retention rule answers as its tombstone."
        ),
        inputs={
            "attestation_id": "str: the record the reference points at",
            "ref": "str | None: the reference's pointer, axiom://attest/sha256:<digest>",
        },
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="attest.search",
        fn=search.run,
        description=(
            "Search the signed records of every log you may read (or one log): words and meaning "
            "together, newest first among equals, narrowed by time, entry type, signer, kind or "
            "interval. Each result says as of when and links to its record. The Timeline and the "
            "Log page search the same way."
        ),
        inputs={
            "query": "str: what to find",
            "site": "str | None",
            "logbook": "str | None: one log only",
            "since": "str | None: ISO 8601, inclusive",
            "until": "str | None: ISO 8601, inclusive",
            "type": "str | list[str] | None: entry type(s) to keep",
            "signer": "str | None: a principal handle, @name:context",
            "kind": "str | list[str] | None: record kinds to keep (statement, supplement, ack, ...)",
            "exclude_kind": "str | list[str] | None: record kinds to leave out, e.g. import",
            "interval": "str | None: kind or kind:number, e.g. run:12",
            "limit": "int: at most 50 (default 20)",
        },
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
)


def _accepting_handles(spec: SkillSpec) -> SkillSpec:
    """A skill that takes ``logbook`` takes a log's handle too, resolved here once (ADR-243).

    A log a site switch turned off (a shift log while shift logs are off) is refused here for
    every such skill: a read answers as for a missing log, a write says in words why."""
    if "logbook" not in (spec.inputs or {}):
        return spec
    inner = spec.fn
    writes = spec.side_effects is not False

    @functools.wraps(inner)
    def fn(params, ctx=None):
        if isinstance(params, dict) and params.get("logbook"):
            from .. import registry, switches

            params = {**params, "logbook": registry.resolve_id(params["logbook"])}
            if registry.switched_off(params["logbook"]):
                from axiom.infra.skills import SkillResult

                why = (switches.SHIFT_LOGS_OFF if writes
                       else f"no logbook {params['logbook']!r} is declared on this node")
                return SkillResult(ok=False, errors=[why])
        return inner(params, ctx)

    return replace(spec, fn=fn)


def _admitted(spec: SkillSpec) -> SkillSpec:
    """A read door's skill asks the one check first and always leaves its read record (LD-1)."""
    from .. import doors

    if spec.name not in DOOR_SKILLS:
        return spec
    from axiom.governance import doors as g

    if g.get(spec.name).shape == "listing":
        return spec  # a listing admits each log inside, by the same check (search.py)
    return replace(spec, fn=doors.admitted_skill(spec.name, spec.fn))


#: The read skills that are doors (``doors.DOORS``): admitted by the one check and recorded.
DOOR_SKILLS = frozenset({
    "attest.show", "attest.intervals", "attest.verify", "attest.export", "attest.export_preview",
    "attest.unacknowledged", "attest.cosign_pending", "attest.seal_due", "attest.seal_preview",
    "attest.obligations", "attest.imports", "attest.access", "attest.filters", "attest.search",
    "attest.reference", "attest.archives", "attest.retention", "attest.rehydrate", "attest.crosschecks",
})  # fmt: skip

SPECS = tuple(_accepting_handles(_admitted(s)) for s in SPECS)


def bind(registry: SkillRegistry) -> None:
    # Signed approvals fill the gate's decision seam here (unit X3); inactive until the
    # approvals_signed flag is on.
    from .. import approvals

    approvals.install()
    for spec in SPECS:
        if not registry.has(spec.name):
            audited = (spec.side_effects is not False or spec.name in AUDITED_READS
                       or spec.name in DOOR_SKILLS)
            registry.register_skill(spec, mutating=audited)


def bind_default() -> SkillRegistry:
    """Bind into the process-default registry (what help, Tab and ``skills emit-md`` read)."""
    registry = default_registry()
    bind(registry)
    return registry


__all__ = ["AUDITED_READS", "DOOR_SKILLS", "SPECS", "bind", "bind_default"]
