# ADR-218: The vault is one component, owned by KEEP, under one noun

**Status:** Accepted (2026-10-28)
**Supersedes (in part):** `secrets` ADR-001 (secrets distinct from KEEP), for the component
and CLI boundary only. Its API boundary (values are not capabilities) stands.
**Related:** ADR-055 (KEEP as Steward + Governor), ADR-056 (CLI verbs are thin wrappers over
skills), ADR-124 (credential truth lives at the issuer), ADR-185 (skill chaining)

## Context

Two built-in extensions handled one person's credentials:

- `secrets` (`axi secrets`) held the values: `set get list rm rotate exposed`, plus setup and
  diagnosis verbs and `secrets vault [setup]`, which says where the vault is.
- `vault` (`axi vault`, KEEP) held the lifecycle: `resolve declare renew sweep steward
  reconcile`, the capability stubs `issue revoke`, and the backups.

A person had to know which noun held which verb, and the split showed through:

- two `audit` verbs (one listed every credential's expiry, the other only the flagged ones,
  with different exit codes) and two `list` verbs (one a stub);
- `axi secrets vault` was about the vault's location and unrelated to `axi vault`;
- `secrets list` and `vault resolve --host` each derived what they showed from the metadata
  on their own. A just-redeemed key appeared in one and not in the other.

## Decision

1. **One component, KEEP, one noun: `axi vault`.** The `vault` extension owns every verb. Its
   help is grouped:
   - **values:** `set get list rm rotate exposed`
   - **lifecycle:** `resolve declare issue revoke renew sweep steward`
   - **records:** `audit` (`--expiry`, `--access`, both by default, in separate sections),
     `reconcile reindex annotate`, and the backup verbs
   - **setup:** `where` (was `secrets vault`), `setup` (was `secrets vault setup`),
     `discover diagnose git-credential wire-git`
2. **Two internal modules, kept apart.** *Storage* (providers, the value store, rotation
   mechanics, discovery) keeps its import path, `axiom.extensions.builtins.secrets`, because
   consumer repositories import it. It no longer has a manifest, a noun or skills of its own.
   *Lifecycle* (capabilities, disposition, renewal, sweep, backups) is
   `axiom.extensions.builtins.vault`. The rule from ADR-001 still applies: KEEP owns policy and
   storage owns mechanism.
3. **One credential index.** `vault.index.CredentialIndex` is the only reader of credential
   metadata for `list`, `resolve` and `audit`. It derives each row's `host`, `purposes` and
   `health` once. `resolve --host H` is defined as `list --host H`, so the two cannot disagree.
   The on-disk index (`<state>/secrets/foreign-credentials.json`) stays where it is, so older
   installs on the same machine read the same file. Version 2 records `host` explicitly. The
   migration from version 1 is additive and idempotent, and it keeps a copy of the version 1
   file.
4. **Deprecation policy.**
   - `axi secrets <verb>` (and `neut secrets …`) keeps working. Each old verb maps to its new
     form and prints one line on stderr naming that form. `AXI_NO_DEPRECATION_WARNING=1`
     silences the line. `secrets audit` keeps its old exit code (a report, never red). The
     alias is hidden from help. **It will be removed in v0.72.0.**
   - The MCP tools are `axiom_vault__*`. The old `axiom_secrets__*` names stay registered for
     one release, with descriptions that begin "DEPRECATED". **They will be removed in
     v0.71.0.**
   - Docs, remedies and error messages name `vault` from now on.

## Consequences

- A person, an agent and `@keep` all address one noun, and the roster entry for KEEP owns one
  extension.
- A writer on an older release rebuilds each record from its own field list, so it can drop
  an explicit `host`. Readers derive the host whenever it is absent, which keeps them in
  agreement. Only an explicitly set host that no field implies can be lost this way.
- Removing the alias is a breaking change for any script still on `secrets`. The stderr line
  is how such scripts get found before v0.72.0.
