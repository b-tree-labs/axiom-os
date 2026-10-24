# ADR-004: Foreign credentials — keychain custody, metadata index, KEEP-executed rotation

Status: Accepted (2026-07-23, issue #667)

## Context

The 2026-07 issuer PAT rotation showed foreign credentials (issuer-minted:
GitLab PATs, webhook URLs, HMAC keys) had no first-class home. The
ergonomic path was plaintext `export`, which metastasized into harness
settings allow-rules, transcripts, and auto-memory. The existing secrets
extension covers *operational backend* stores (OpenBao, cloud managers);
KEEP's vault covers *Axiom-minted* capabilities. Neither stored or
rotated a third-party PAT on a developer workstation.

## Decision

1. **Two planes.** Values live in an OS-keychain-backed
   `SecretStoreProvider` (`keychain`, darwin `security` CLI; `file` 0600
   JSON as dev/CI fallback only). Metadata — name, RotationProvider
   kind, issuer URL, expiry, git wiring — lives in a 0600 JSON index
   (`<state>/secrets/foreign-credentials.json`). List/audit paths read
   the index only and never open the keychain.
2. **RotationProvider factory** (`foreign/rotation_providers.py`),
   distinct from the backend `RotationStrategy` layer: providers know
   *issuers*. `gitlab-pat` drives
   `POST /api/v4/personal_access_tokens/self/rotate` (GitLab >= 16.0,
   default expiry +1 week); `guided` is the interactive paste fallback
   and fails closed headless.
3. **KEEP executes rotation** through the deterministic
   `secrets.rotate` skill: read current → issuer rotate → store new →
   probe-verify → report scrub candidates (location *types*; never
   auto-edited). The whole exchange runs inside `guarded_act`
   (agent `keep`, op class `secrets.rotate`) and journals to the #665
   action ledger — metadata and issuer handles only.
4. **MCP exposure on the memory server** (until #669's composed
   server): `axiom_secrets_list`, `axiom_vault_audit`,
   `axiom_secrets_rotate_trigger`. Invariant: secret values never
   transit MCP; the rotation exchange is process-internal.
5. **Consumption without plaintext:** `axi secrets git-credential`
   implements the git credential-helper protocol against the store;
   `wire-git` configures it per host. Values enter via stdin/prompt
   only — never argv.
6. **Extension secret dependencies:** `[extension.secrets] requires =
   [...]` in the AEOS manifest; `axi ext lint` validates shape
   (AEOS080, error) and local presence (AEOS081, warning).

## Consequences

- `axi secrets rotate <name>` (bare name) routes to the foreign flow;
  `scheme://path` refs keep the pre-existing backend-strategy behavior.
- Expiry surfaces through `secrets audit` / `axiom_vault_audit`;
  proactive rotate-before-expiry scheduling is deferred to the KEEP
  scheduling follow-up issue.
- Linux secret-service custody is a follow-up; until then non-darwin
  hosts use an explicit backend override (`AXIOM_FOREIGN_SECRETS_BACKEND`)
  or the dev-only `file` fallback.
