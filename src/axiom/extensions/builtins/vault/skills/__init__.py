# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""KEEP's skill surface: every verb of the one vault component (ADR-218, ADR-056).

The canonical names are ``vault.*``. They come in four groups, which is also
how ``axi vault --help`` lists them:

* values      set get list rm rotate exposed
* lifecycle   resolve declare renew sweep (issue revoke steward are CLI verbs)
* records     audit reconcile reindex annotate, and the backup verbs
* setup       where setup discover diagnose git_credential wire_git

The value-handling functions live in the storage module
(``axiom.extensions.builtins.secrets``, whose import path is kept for the
repositories that import it); this package registers them under ``vault``.

``secrets.*`` names are registered too, as DEPRECATED aliases of the canonical
skills (see :data:`SKILL_ALIASES`), so ``axiom_secrets__*`` MCP tools and any
code that invokes ``secrets.<verb>`` keep working for one release.
"""

from axiom.infra.skill_declarations import NextStep, Precondition, Ref
from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from . import audit, backup, declare, deprecated, reconcile, renew, resolve, sweep, values

_NAMESPACE = "vault"

#: The release that drops the deprecated names (ADR-218).
MCP_ALIAS_REMOVED_IN = "v0.71.0"
CLI_ALIAS_REMOVED_IN = "v0.72.0"


def _credential_exists(params, ctx) -> tuple[bool, str]:
    """Cheap and side-effect free: reads the metadata index, never a value."""
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
    from axiom.infra.skills import ensure_context

    name = str((params or {}).get("name") or "").strip()
    if not name:
        return False, "no credential name was given"
    return ForeignCredentialStore(ensure_context(ctx).state_dir).exists(name), f"no credential named {name!r} is stored"


_CREDENTIAL_EXISTS = Precondition(
    "credential-exists",
    "the credential must already be in the store",
    check=_credential_exists,
    consumes="credential_name",
    param="name",
    fix="list the stored names with 'axi vault list', then pass one as name; add a missing one with 'axi vault set {name}'",
)

# Declared specs (ADR-063 / ADR-072 §4.9.4). Until now KEEP registered through
# the legacy ``register(name, fn)`` form, so its verbs carried no description
# and no surfaces — which means they were invisible to MCP, to chat, to the
# agent tool loop and to the capability discovery block. An assistant asked to
# deal with an expiring credential had no way to learn these existed, and
# reached for a raw git credential helper instead.
#
# Only the READ verbs are projected. They report metadata and never a
# credential value, which is what makes them safe on a shared surface; rotation
# and sweeps stay CLI-only where a human is present.
_SPECS = (
    SkillSpec(
        name="vault.resolve",
        fn=resolve.run,
        description=(
            "Which stored credential to use for a host, and whether it is still "
            "alive. A host often has several (git-scoped, API-scoped, per-project) "
            "and the git credential helper is keyed by host alone, so it silently "
            "returns one of them. Call this before reaching for a credential "
            "helper. Returns names, purposes and expiry health — never a value."
        ),
        inputs={
            "host": "hostname, e.g. git.example.org",
            "purpose": "git|api|registry|runner (optional; omit to list every candidate)",
        },
        side_effects=False,
        idempotent=True,
        surfaces=("cli", "mcp", "agent_tool"),
        risk="read",
        requires=(),
        produces=(),
        next_steps=(),
    ),
    SkillSpec(
        name="vault.reconcile",
        fn=reconcile.run,
        description=(
            "Compare what the credential store RECORDS against what each "
            "issuer actually says: revoked, real expiry, or a value the issuer "
            "no longer recognises. Expiry auditing only reads our own metadata, "
            "which is what we believed when we wrote it down. Metadata only, "
            "never a value."
        ),
        inputs={"apply": "record the issuer's expiry locally (metadata only)"},
        side_effects=False,
        idempotent=True,
        surfaces=("cli", "mcp", "agent_tool"),
        risk="read",
        requires=(),
        produces=(),
        next_steps=(
            NextStep("vault.audit", "ok", "see which credentials still need a disposition or a renewal"),
        ),
        recipes=("credential-health-review", "credential-leak-response"),
    ),
    SkillSpec(
        name="vault.declare",
        fn=declare.run,
        description=(
            "Record what can be done about a credential — self_rotatable, "
            "human_rotatable, externally_owned or non_expiring — plus a review "
            "date or owner. This is what makes an undated credential's finding "
            "clearable instead of repeating forever."
        ),
        inputs={
            "name": "credential name",
            "disposition": "self_rotatable|human_rotatable|externally_owned|non_expiring",
            "review_by": "YYYY-MM-DD, for credentials with no expiry",
            "owner": "who to contact, required for externally_owned",
        },
        # WRITES metadata, so it is declared but NOT projected to a protocol
        # surface: a write on MCP is a per-verb decision, not a default.
        side_effects=True,
        idempotent=True,
        surfaces=("cli",),
        risk="reversible-write",
        requires=(_CREDENTIAL_EXISTS,),
        produces=(),
        next_steps=(NextStep("vault.audit", "ok", "confirm the finding cleared"),),
        recipes=("credential-finding-clearance",),
    ),
    SkillSpec(
        name="vault.audit",
        fn=audit.run,
        description=(
            "The vault's records in two sections. expiry: every credential's health "
            "(expired, expiring soon, or no recorded expiry at all), with the flagged "
            "ones listed. access: who called the vault's verbs, over which surface "
            "(partial: only calls that went through the dispatcher). Both by default; "
            "pass expiry or access to choose one. Metadata only, never a value."
        ),
        inputs={
            "within_days": "expiry horizon in days (default 14)",
            "expiry": "include the expiry section (default: both sections)",
            "access": "include the access section (default: both sections)",
        },
        side_effects=False,
        idempotent=True,
        surfaces=("cli", "mcp", "agent_tool", "skill_md"),
        risk="read",
        requires=(),
        produces=(
            Ref(
                "credential_name",
                "the first flagged credential; pass as name to vault.declare",
                field="credential_name",
            ),
        ),
        next_steps=(
            NextStep(
                "vault.declare",
                "produced:credential_name",
                "record what can be done about the flagged credential so its finding can clear",
                {"name": "credential_name"},
            ),
            NextStep("vault.renew", "error", "rotate what may rotate itself before it lapses"),
        ),
        recipes=("credential-finding-clearance", "credential-health-review"),
    ),
    SkillSpec(
        name="vault.renew",
        fn=renew.run,
        description=(
            "Rotate the credentials that are due and permitted to rotate themselves, "
            "stating an explicit expiry on each; anything needing a person fails the "
            "pass visibly. Metadata only, never a value."
        ),
        inputs={},
        side_effects=True,
        idempotent=True,
        surfaces=("cli",),
        risk="reversible-write",
        requires=(),
        produces=(),
        next_steps=(NextStep("vault.audit", "ok", "confirm nothing is left due"),),
    ),
    SkillSpec(
        name="vault.sweep",
        fn=sweep.run,
        description=(
            "Look for credential material that is not under management and escalate "
            "what it finds. Reports findings; never stores or prints a value."
        ),
        inputs={},
        side_effects=True,
        idempotent=True,
        surfaces=("cli",),
        risk="reversible-write",
        requires=(),
        produces=(),
        next_steps=(),
    ),
)
# Vault backup (axiom.infra.vault_backup).  Declared specs, CLI/agent surfaces only: a backup verb
# on MCP is a per-verb decision, and these handle the material the whole vault exists to protect.
_BACKUP_ID = Ref(
    "backup_id",
    "pass as id to vault.backup_show, vault.backup_verify, vault.backup_pin or vault.backup_restore",
    field="id",
)

_BACKUP_KEY = Precondition(
    "backup-key",
    "a backup key must exist, because backups are never written in the clear",
    check=backup.key_configured,
    fix="create one with 'axi vault key-init' (it shows the recovery code once), then check it with 'axi vault key-info'",
)

_BACKUP_EXISTS = Precondition(
    "backup-exists",
    "the backup must be in the store",
    check=backup.backup_exists,
    consumes="backup_id",
    param="id",
    fix="list the backups with 'axi vault ls' and pass one of its ids as id; make one with 'axi vault backup'",
)

_RECOVERY_CODE = Precondition(
    "recovery-code-exists",
    "the recovery code (or passphrase) must be supplied, because a backup can only be opened with it",
    check=backup.recovery_code_given,
    fix="run it in a terminal to be asked for the recovery code, or pass `--code-file PATH` (a file only you can read); "
        "if the code is lost, 'axi vault key-info' says what that means and what can still be recovered",
)

_BACKUP_SPECS = (
    SkillSpec(name="vault.backup", fn=backup.backup,
              description="Back up the credential values the harness holds into one encrypted archive. "
                          "Skips when nothing changed. Never prints a value.",
              inputs={"to": "directory (e.g. another disk)", "force": "even if unchanged",
                      "passphrase": "encrypt to a passphrase instead of the configured key"},
              side_effects=True, idempotent=True, surfaces=("cli",),
              risk="reversible-write", requires=(_BACKUP_KEY,),
              produces=(_BACKUP_ID,),
              next_steps=(
                  NextStep("vault.backup_verify", "produced:backup_id", "prove the new backup is intact",
                           feeds={"id": "backup_id"}),
                  NextStep("vault.key_info", "ok", "check custody: where the backups are and which code opens them"),
                  NextStep("vault.key_init", "error", "create the backup key when none exists"),
              )),
    SkillSpec(name="vault.backup_ls", fn=backup.ls, description="List vault backups (names and counts only).",
              inputs={"from_dir": "backup directory"}, side_effects=False, idempotent=True, surfaces=("cli",),
              risk="read",
              next_steps=(
                  NextStep("vault.backup_show", "ok", "open one backup's manifest"),
                  NextStep("vault.backup", "ok", "take a backup if the newest is old"),
              )),
    SkillSpec(name="vault.backup_show", fn=backup.show,
              description="Show one backup's manifest: credential names and fingerprints, no values, no key needed.",
              inputs={"id": "backup id or 'latest'"}, side_effects=False, idempotent=True, surfaces=("cli",),
              risk="read", requires=(_BACKUP_EXISTS,),
              produces=(_BACKUP_ID,),
              next_steps=(
                  NextStep("vault.backup_verify", "produced:backup_id", "prove it is intact", feeds={"id": "backup_id"}),
                  NextStep("vault.backup_restore", "produced:backup_id",
                           "restore from it (opens a plan; a person approves)", feeds={"id": "backup_id"}),
              )),
    SkillSpec(name="vault.backup_verify", fn=backup.verify,
              description="Verify the backup store: ledger chain, rollback evidence, file hashes; deep = decrypt.",
              inputs={"id": "optional backup", "deep": "decrypt and compare fingerprints (needs the recovery code)"},
              side_effects=False, idempotent=True, surfaces=("cli",),
              risk="read",
              next_steps=(
                  NextStep("vault.backup", "error", "make a fresh backup when this one is damaged"),
                  NextStep("vault.key_info", "ok", "check custody of the recovery code"),
              )),
    SkillSpec(name="vault.backup_restore", fn=backup.restore,
              description="Restore credentials from a backup. Dry run unless apply; never overwrites silently; "
                          "backs up the current vault first.",
              inputs={"id": "backup id or 'latest'", "only": "name globs", "apply": "really write",
                      "overwrite": "replace conflicting values", "include_tainted": "restore exposed values anyway",
                      "skip_pre_backup": "do not back up the current vault first", "force": "override guards"},
              side_effects=True, idempotent=False, surfaces=("cli",),
              # Writes credential values back into the vault. The current vault is backed up first unless
              # skip_pre_backup says otherwise; with that and overwrite the replaced values are gone.
              risk="destructive", requires=(_RECOVERY_CODE, _BACKUP_EXISTS),
              produces=(Ref("pre_restore_id", "the safety backup taken first; pass as id to vault.backup_restore to undo"),),
              next_steps=(
                  NextStep("vault.backup_restore", "produced:pre_restore_id",
                           "undo this restore by restoring the safety backup (opens a plan)",
                           feeds={"id": "pre_restore_id"}),
                  NextStep("vault.audit", "ok", "check which credentials are healthy now"),
                  NextStep("vault.key_info", "error", "check the recovery code is the one these backups were made with"),
              )),
    SkillSpec(name="vault.backup_prune", fn=backup.prune,
              description="Apply vault-backup retention (dry run unless apply).",
              inputs={"apply": "really delete"}, side_effects=True, idempotent=True, surfaces=("cli",),
              # Deleted backups cannot be recovered (pinned ones are exempt). Classified by the apply form.
              risk="destructive", requires=(),
              next_steps=(NextStep("vault.backup_ls", "ok", "see what is left"),)),
    SkillSpec(name="vault.backup_pin", fn=backup.pin, description="Exempt a backup from pruning.",
              inputs={"id": "backup id"}, side_effects=True, idempotent=True, surfaces=("cli",),
              risk="reversible-write", requires=(_BACKUP_EXISTS,),
              next_steps=(NextStep("vault.backup_unpin", "always", "make it prunable again when it is no longer needed"),)),
    SkillSpec(name="vault.backup_unpin", fn=backup.unpin, description="Make a pinned backup prunable again.",
              inputs={"id": "backup id"}, side_effects=True, idempotent=True, surfaces=("cli",),
              risk="reversible-write", requires=(_BACKUP_EXISTS,)),
    SkillSpec(name="vault.backup_scrub", fn=backup.scrub,
              description="Remove an exposed credential's old value from past backups (re-encrypts them), or "
                          "mark them tainted when no key is available.",
              inputs={"name": "credential", "all_versions": "remove every stored version"},
              side_effects=True, idempotent=True, surfaces=("cli",),
              # Removes text from archives and cannot put it back. Classified by the rewriting form.
              risk="destructive",
              requires=(Precondition(
                  "credential-named", "the credential to scrub must be named",
                  check=backup.credential_named,
                  fix="pass name=<credential>; list stored names with 'axi vault list'"),),
              next_steps=(NextStep("vault.backup_verify", "ok", "prove the rewritten backups still open"),),
              recipes=("credential-leak-response",)),
    SkillSpec(name="vault.key_info", fn=backup.key_info,
              description="Whether a backup key exists, its fingerprint and custody reminders. Never the key.",
              side_effects=False, idempotent=True, surfaces=("cli",),
              risk="read",
              next_steps=(
                  NextStep("vault.key_init", "ok", "create the backup key if the report says none exists"),
                  NextStep("vault.backup_verify", "ok", "prove the code you hold still opens the backups"),
              )),
    SkillSpec(name="vault.key_init", fn=backup.key_init,
              description="Create the backup key; shows the recovery code once.",
              inputs={"passphrase": "use a passphrase you choose", "force": "replace an existing key"},
              side_effects=True, idempotent=False, surfaces=("cli",),
              # force replaces the key new backups use; archives made under the old key still open with its code.
              risk="reversible-write",
              requires=(Precondition(
                  "somewhere-to-show-the-code",
                  "the recovery code is shown once, so there must be somewhere to show it (or a passphrase of your choosing)",
                  check=backup.somewhere_to_show_the_code,
                  fix="run it in a terminal, or pass code_out=<file only you can read>, or pass passphrase"),),
              next_steps=(
                  NextStep("vault.backup", "ok", "take the first backup"),
                  NextStep("vault.key_info", "ok", "confirm the key and where custody stands"),
              )),
    SkillSpec(name="vault.backup_tick", fn=backup.tick,
              description="Scheduled vault backup: runs when due and something changed. Never prompts.",
              side_effects=True, idempotent=True, surfaces=("cli",),
              # Needs a key too, but says so loudly itself (a backup job that fails quietly is the failure
              # mode), so the refusal text stays the function's own.
              risk="reversible-write", requires=()),
)
#: Audited through the universal action chain: they change the vault, the key or past backups.
_BACKUP_AUDITED = {"vault.backup_restore", "vault.backup_scrub", "vault.key_init", "vault.backup_prune"}

# --------------------------------------------------------------------------- values + setup
#
# The verbs that handle the credentials themselves, and the ones that set the
# vault up. Their functions live in the storage module; the policy of who may
# reach each one is here, because ADR-155 makes ``surfaces`` the enforcement
# point for what an agent may do about a credential:
#
#   * ``set`` and ``rm`` are CLI-ONLY at every authority level. One writes a
#     value the caller chose, the other destroys the only copy.
#   * ``get`` is CLI-only for the same reason in reverse: it returns a value.
#   * ``where`` and ``setup`` are CLI-only: they decide which vault every
#     process on this machine trusts, which is a person's call.
#
# ``exposed`` is ``reversible-write`` and NOT ``destructive``: it is reachable
# by an agent on purpose, because the clock matters more than the ceremony when
# a credential has leaked (ADR-155 §3). It records the exposure first and
# rotates second, issuing a new version rather than deleting the old one.


def _has(param: str, why: str):
    """A cheap presence check for a required parameter, with the reason it failed."""

    def check(params, ctx):
        return (bool((params or {}).get(param)), why)

    return check


_ALL = ("cli", "mcp", "agent_tool", "skill_md")
_AGENT = ("cli", "mcp", "agent_tool")
_CLI = ("cli",)


def _storage(name, fn, description, *, surfaces, idempotent, side_effects, risk, inputs=None, **chain):
    return SkillSpec(
        name=f"vault.{name}", fn=fn, description=description, inputs=dict(inputs or {}),
        side_effects=side_effects, idempotent=idempotent, surfaces=surfaces, risk=risk,
        requires=chain.pop("requires", ()), produces=chain.pop("produces", ()),
        next_steps=chain.pop("next_steps", ()), **chain,
    )


_STORAGE_SPECS = (
    # values
    _storage("set", values.set_credential, "Store a credential. The value comes from stdin or a hidden prompt, never argv.",
             surfaces=_CLI, idempotent=False, side_effects=True, risk="destructive",
             inputs={"name": "credential name", "issuer_url": "issuer base URL", "host": "the host it is for",
                     "expires_at": "YYYY-MM-DD", "notes": "free-form note"}),
    _storage("get", values.get_credential, "A credential's metadata; its value only with reveal (break-glass).",
             surfaces=_CLI, idempotent=True, side_effects=False, risk="read", inputs={"name": "credential name"}),
    _storage("list", values.list_credentials,
             "Every stored credential with its host, purposes and expiry health, from the one credential "
             "index; host narrows it to the credentials vault.resolve would consider. Never a value.",
             surfaces=_ALL, idempotent=True, side_effects=False, risk="read",
             inputs={"host": "only the credentials for this host (the same set vault.resolve reads)"}),
    _storage("rm", values.remove_credential, "Delete a credential: its value and its metadata. Confirmed.",
             surfaces=_CLI, idempotent=False, side_effects=True, risk="destructive", inputs={"name": "credential name"}),
    # Not idempotent: running it twice issues two credentials and retires one
    # that something may still be holding.
    _storage("rotate", values.rotate, "Rotate one credential now (force is the leaked-key closer). Never prints a value.",
             surfaces=_AGENT, idempotent=False, side_effects=True, risk="reversible-write",
             inputs={"ref": "a SecretRef or a stored credential name"}),
    _storage("exposed", values.exposed,
             "A credential appeared somewhere it should not have: record the exposure, then force-rotate it. "
             "Never prints a value.",
             surfaces=_AGENT, idempotent=False, side_effects=True, risk="reversible-write",
             inputs={"ref": "the credential that leaked, as a reference or a stored name",
                     "where": "the surface it appeared on (transcript, log, chat, url, ...)"},
             requires=(
                 Precondition(
                     "ref",
                     "the secret that leaked, as a reference (never the value)",
                     check=_has("ref", "no ref was given"),
                     fix="pass ref=<scheme>://<path> or a stored name; list stored names with 'axi vault list'",
                 ),
                 Precondition(
                     "where",
                     "the surface the credential appeared on",
                     check=_has("where", "no surface was given"),
                     fix="pass where=<transcript|log|chat|url|...>, the place the value was seen",
                 ),
             ),
             next_steps=(
                 NextStep("vault.reconcile", "ok", "confirm the issuer now treats the old value as dead"),
             ),
             recipes=("credential-leak-response",)),
    # records
    # Opens stored values to backfill fingerprints, and migrates the index file.
    _storage("reindex", values.reindex,
             "Bring the credential index to its current version and backfill missing fingerprints. "
             "Prints counts only.",
             surfaces=_CLI, idempotent=True, side_effects=True, risk="reversible-write",
             inputs={"dry_run": "report what would change, open nothing"}),
    # Metadata only. Still CLI-only: an agent must not rewrite expiry on a
    # credential a person is accountable for.
    _storage("annotate", values.annotate, "Record expiry, host or notes on a stored credential without replacing it.",
             surfaces=_CLI, idempotent=True, side_effects=True, risk="reversible-write",
             inputs={"name": "credential name", "expires_at": "YYYY-MM-DD", "host": "the host it is for",
                     "notes": "free-form note"}),
    # setup
    _storage("where", values.where,
             "Where this machine's vault is and how each setting resolves; with backend, write the settings "
             "file programs started outside a terminal read. Never a token.",
             surfaces=_CLI, idempotent=True, side_effects=True, risk="reversible-write",
             # The token is named by its file (a CLI flag); it is not listed as an input, so no
             # surface that renders inputs shows a token-shaped field.
             inputs={"backend": "e.g. openbao", "url": "the vault's address", "mount": "the KV mount"}),
    _storage("setup", values.setup,
             "Start this computer's local vault (macOS, Linux or Windows) and, if you agree, point your "
             "shells at it.",
             surfaces=_CLI, idempotent=True, side_effects=True, risk="reversible-write",
             inputs={"shell": "shells to configure", "yes": "edit profiles without asking"}),
    _storage("discover", values.discover,
             "Find credential material living outside the vault (git remotes, credential files, env files). "
             "Reports fingerprints, never values.",
             surfaces=_ALL, idempotent=True, side_effects=False, risk="read"),
    _storage("diagnose", values.diagnose,
             "Probe one secret reference end to end, or every registered backend. Never prints a value.",
             surfaces=_ALL, idempotent=True, side_effects=False, risk="read",
             inputs={"ref": "a SecretRef to probe; omit to walk every backend"}),
    # git's credential protocol speaks on stdin/stdout; there is no other
    # surface it could have.
    _storage("git_credential", values.git_credential, "The git credential-helper protocol (get, store, erase).",
             surfaces=_CLI, idempotent=True, side_effects=False, risk="read"),
    _storage("wire_git", values.wire_git, "Point git's credential lookup for a host at a stored credential.",
             surfaces=_CLI, idempotent=False, side_effects=True, risk="reversible-write",
             inputs={"name": "credential name", "host": "git host"}),
)


# --------------------------------------------------------------------------- deprecated aliases
#
# ``secrets.<verb>`` -> the canonical skill, and the parameters the alias fixes
# (applied in ``deprecated.py``, which holds one function per old name).
# One release (ADR-218): the MCP tools ``axiom_secrets__*`` go in
# MCP_ALIAS_REMOVED_IN; the ``axi vault`` noun, which rides these, in
# CLI_ALIAS_REMOVED_IN.
SKILL_ALIASES: dict[str, tuple[str, dict]] = {
    "secrets.diagnose": ("vault.diagnose", {}),
    "secrets.discover": ("vault.discover", {}),
    "secrets.exposed": ("vault.exposed", {}),
    "secrets.rotate": ("vault.rotate", {}),
    "secrets.set": ("vault.set", {}),
    "secrets.list": ("vault.list", {}),
    "secrets.rm": ("vault.rm", {}),
    "secrets.get": ("vault.get", {}),
    # The old secrets.audit was the expiry report alone, and it never failed.
    "secrets.audit": ("vault.audit", {"expiry": True, "access": False, "report_only": True}),
    "secrets.reindex": ("vault.reindex", {}),
    "secrets.git_credential": ("vault.git_credential", {}),
    "secrets.wire_git": ("vault.wire_git", {}),
    "secrets.annotate": ("vault.annotate", {}),
    # `secrets vault` was both: action=setup started the vault, else it said where it was.
    "secrets.vault": ("vault.where", {}),
}


def canonical_skill_name(name: str) -> str:
    """The canonical ``vault.*`` name for ``name``; ``name`` itself when it is not an alias."""
    target = SKILL_ALIASES.get(name)
    return target[0] if target else name


def _alias_spec(old: str, target: SkillSpec) -> SkillSpec:
    from dataclasses import replace

    new = canonical_skill_name(old)
    tool = "axiom_" + new.replace(".", "__")
    return replace(
        target,
        name=old,
        fn=getattr(deprecated, old.replace(".", "_")),
        description=(
            f"DEPRECATED: use {new} (MCP tool {tool}); this name is removed in "
            f"{MCP_ALIAS_REMOVED_IN}. " + (target.description or "")
        ).strip(),
        # A deprecated name gets no generated SKILL.md of its own.
        surfaces=tuple(x for x in (target.surfaces or ()) if x != "skill_md") or None,
    )


# Every CLI verb maps 1:1 to a registered skill (ADR-056). `renew` was
# absent here, so the verb that rotates an expiring credential was a
# CLI-private function: no authority gate, no audit record, no telemetry,
# and invisible to MCP, chat and agents. It crashed for weeks and left no
# trace anywhere that anyone reads.
#
# `declare`, `renew` and `sweep` are audited: they are the verbs here that act
# (declare writes credential metadata), as opposed to report. The storage verbs
# keep the default (audited) they always had.
_AUDITED = {"vault.declare", "vault.renew", "vault.sweep"}


def _all_specs() -> tuple[SkillSpec, ...]:
    return _STORAGE_SPECS + _SPECS + _BACKUP_SPECS


def _mutating(name: str) -> bool:
    if name in _AUDITED or name in _BACKUP_AUDITED:
        return True
    if any(spec.name == name for spec in _STORAGE_SPECS):
        return True
    return False


def bind(registry: SkillRegistry) -> None:
    """Register every vault skill, then the deprecated ``secrets.*`` aliases. Idempotent."""
    by_name = {}
    for spec in _all_specs():
        by_name[spec.name] = spec
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=_mutating(spec.name))
    for old, (new, _fixed) in SKILL_ALIASES.items():
        if not registry.has(old):
            registry.register_skill(_alias_spec(old, by_name[new]), mutating=_mutating(new))


def bind_default() -> SkillRegistry:
    """Bind into the process-local default registry; idempotent."""
    reg = default_registry()
    bind(reg)
    return reg


def verbs() -> list[str]:
    """Canonical skill verbs (no namespace prefix)."""
    return [spec.name.split(".", 1)[1] for spec in _all_specs()]


__all__ = [
    "CLI_ALIAS_REMOVED_IN",
    "MCP_ALIAS_REMOVED_IN",
    "SKILL_ALIASES",
    "bind",
    "bind_default",
    "canonical_skill_name",
    "verbs",
]
