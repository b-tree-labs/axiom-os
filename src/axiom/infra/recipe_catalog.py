# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The recipes this platform publishes (ADR-185).

Each is a vetted chain of skills. A step that names a skill which does not
exist on this base is a *placeholder*: it is declared in ``placeholders`` so
``recipe.check`` reports it as missing and the sweep knows exactly what to fill.
The list only shrinks; ``check`` fails if a placeholder has since appeared.

Still placeholders (no skill exists for them on this base):
  chat.renew, chat.compact                  - session-handoff (the chat commands that change a
                                              session have no registered skill form; only reads do)

Every other step is a real skill, and ``recipe.check`` fails if one of them goes away.
"""

from __future__ import annotations

from axiom.infra.recipes import Recipe, RecipeStep

INCIDENT_RESPONSE = Recipe(
    id="incident-response",
    intent="Contain a misbehaving process, capture the state around it, and prepare the way back.",
    params={"match": "which process to look for (name or pattern)"},
    steps=(
        RecipeStep(
            "list",
            "process.ls",
            "find the process and read its identity before touching it",
            inputs={"match": "$match"},
        ),
        RecipeStep(
            "stop",
            "process.stop",
            "stop it - this opens a plan; nothing stops until a person approves (kill=true is what makes the apply real)",
            inputs={"id": "$list.process_id"},
            approval=True,
            undo="start it again from the same definition; the snapshot in the next step records what it was doing",
        ),
        RecipeStep(
            "snapshot",
            "state.snapshot",
            "capture the state as it is now, before anything else changes it",
            inputs={"reason": "incident-response"},
            undo="a snapshot only adds; prune it when the incident is closed",
        ),
        RecipeStep(
            "restore",
            "state.restore",
            "prepare the restore - this opens a plan; nothing is restored until a person approves",
            inputs={"id": "$snapshot.snapshot_id"},
            approval=True,
            undo="snapshot first; a restore replaces current state",
        ),
    ),
    rollback="Restore the snapshot taken in step 3 (or the safety snapshot a restore reports) and restart the stopped process.",
)

CREDENTIAL_LEAK_RESPONSE = Recipe(
    id="credential-leak-response",
    intent="Close a leaked credential: record the exposure and rotate it, scrub where it landed, and confirm with the issuer.",
    params={
        "ref": "the secret reference that leaked (never the value)",
        "where": "the surface it appeared on (transcript, log, chat, url)",
    },
    steps=(
        RecipeStep(
            "exposed",
            "secrets.exposed",
            "record the exposure, then force-rotate; the clock matters more than the ceremony",
            inputs={"ref": "$ref", "where": "$where"},
            undo="rotation cannot be undone and should not be; the old value stays retired",
        ),
        RecipeStep(
            "scrub",
            "vault.backup_scrub",
            "remove the leaked value from past vault backups (re-encrypts them) - this opens a plan",
            inputs={"name": "$ref"},
            approval=True,
            undo="a scrub removes text it cannot restore; the plan lists exactly what it will touch",
        ),
        RecipeStep(
            "panic",
            "process.panic",
            "dry run only: list what a panic stop would halt, so a person can decide (kill is not passed, so nothing is halted)",
            approval=True,
        ),
        RecipeStep(
            "confirm",
            "vault.reconcile",
            "compare what the store records with what the issuer says, to confirm the old value is dead",
        ),
    ),
    rollback="There is no way back from a leak; if rotation failed, rerun secrets.exposed once the backend is reachable.",
)

SESSION_HANDOFF = Recipe(
    id="session-handoff",
    intent="Hand a working session to another harness without losing its thread.",
    params={"session": "the session to hand off"},
    steps=(
        RecipeStep(
            "renew",
            "chat.renew",
            "renew the session so its context is fresh and sourced",
            inputs={"session": "$session"},
            expects=("session_id",),
        ),
        RecipeStep(
            "where",
            "chat.where",
            "state where the session is: its identity, store and what resumes it",
            inputs={"session": "$renew.session_id"},
        ),
        RecipeStep(
            "compact",
            "chat.compact",
            "compact the session to the part the next harness needs",
            inputs={"session": "$renew.session_id"},
            undo="compaction keeps the original transcript; resume from it to recover detail",
        ),
    ),
    rollback="Resume the original session; compaction never deletes it.",
    placeholders=frozenset({"chat.renew", "chat.compact"}),
)

MEMORY_VERIFY = Recipe(
    id="memory-verify",
    intent="Check a remembered claim against where it came from before relying on it.",
    params={"query": "the claim or topic to verify"},
    steps=(
        RecipeStep(
            "search",
            "chat.memory_search",
            "find the remembered statement",
            inputs={"query": "$query"},
        ),
        RecipeStep(
            "open",
            "chat.memory_show",
            "open it and read where it came from, then compare that with the claim",
            inputs={"id": "$search.fragment_id"},
        ),
    ),
    rollback="Nothing to undo; both steps read.",
)

CREDENTIAL_FINDING_CLEARANCE = Recipe(
    id="credential-finding-clearance",
    intent="Clear a credential finding: find what is flagged, record what can be done about it, and confirm the finding is gone.",
    params={"disposition": "self_rotatable, human_rotatable, externally_owned or non_expiring"},
    steps=(
        RecipeStep(
            "audit",
            "vault.audit",
            "list the credentials that are expired, expiring or have no recorded expiry",
            inputs={"within_days": "14"},
        ),
        RecipeStep(
            "declare",
            "vault.declare",
            "record what can be done about the first flagged credential",
            inputs={"name": "$audit.credential_name", "disposition": "$disposition"},
            undo="declare it again with a different disposition",
        ),
        RecipeStep(
            "verify",
            "vault.audit",
            "run the audit again; the finding should be gone or reduced to what still needs a person",
        ),
    ),
    rollback="Declaring only records metadata; set a different disposition to change it.",
)

CREDENTIAL_HEALTH_REVIEW = Recipe(
    id="credential-health-review",
    intent="Check that every stored credential is both recorded as healthy and still alive at its issuer.",
    steps=(
        RecipeStep(
            "audit",
            "vault.audit",
            "read what the store records: expired, expiring, or no expiry on file",
            inputs={"within_days": "14"},
        ),
        RecipeStep(
            "reconcile",
            "vault.reconcile",
            "compare those records with what each issuer says, since a recorded expiry is only what we believed",
        ),
    ),
    rollback="Nothing to undo; both steps read.",
)

RECIPES: tuple[Recipe, ...] = (
    CREDENTIAL_FINDING_CLEARANCE,
    CREDENTIAL_HEALTH_REVIEW,
    CREDENTIAL_LEAK_RESPONSE,
    INCIDENT_RESPONSE,
    MEMORY_VERIFY,
    SESSION_HANDOFF,
)

__all__ = ["RECIPES"]
