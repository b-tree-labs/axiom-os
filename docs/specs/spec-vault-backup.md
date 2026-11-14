# Spec: Vault Backup

**Owner:** platform • **Status:** Active • **Last updated:** 2026-10-08
**PRD:** none (operator-safety capability; companion to `spec-state-snapshots.md`) • **Key ADRs:** ADR-056 (CLI verbs are thin wrappers over skills), ADR-055 (the vault extension)

## Overview

State snapshots (`spec-state-snapshots.md`) deliberately never copy credential
values: they record names only. That is the right default for a store that is
copied around freely, and it leaves a gap. Lose the machine, the keychain or a
disk, and every credential the harness itself held is gone, with nothing to
restore them from.

Vault backup closes the gap with the opposite trade: it **does** copy values,
and compensates with a different class of protection. Every backup is one
authenticated-encrypted archive. The key that opens it is never stored beside
it. Output, logs, manifests and the audit trail name credentials and carry
one-way fingerprints, and never a value, a key, a passphrase or a recovery
code.

```
 set / rm / rotate / exposed ──┐ (debounced)
 schedule (daily, 30-min tick) ─┼─> collect ──> seal (public key only) ──> write 0600, atomic
 on demand: backup              │                       │                        │
 before every restore ──────────┘                       └─ verify-on-write <─────┤
                                                                                 v
        restore <── needs the RECOVERY CODE (offline)               ledger.jsonl (hash chain)
        scrub   <── needs the RECOVERY CODE                         head recorded outside the dir
```

Code: `src/axiom/infra/vault_backup/` (engine), `src/axiom/extensions/builtins/vault/`
(the `vault` noun: `backup_cli.py`, `skills/backup.py`, schedule), the seam
`src/axiom/extensions/builtins/secrets/foreign/backup_hook.py`, and
`scripts/axi-restore.py vault ...` (emergency path; `axi-vault-restore.py` is a one-release shim). The
lifecycle shared with state snapshots and pg dumps (atomic writes, hash-chained ledger and its external
head, lock, disk checks, retention, `latest` selection, fault injection) is `src/axiom/infra/backup_core.py`.

## Survey: where the harness keeps credentials

| Store | What it is | Where the values live | Scope |
|---|---|---|---|
| Foreign credential store (`secrets/foreign/store.py`), `keychain` backend | Named third-party credentials; default on macOS (Windows Credential Manager uses the same class of backend) | The OS keychain, service `axiom-secrets`, account = credential name. Metadata index `<state>/secrets/foreign-credentials.json` (0600; names, issuer, expiry, fingerprint, no values) | **In scope.** Read and written only through `ForeignCredentialStore` |
| Foreign store, `file` backend (`AXIOM_FOREIGN_SECRETS_BACKEND=file`, dev/CI/Linux fallback) | 0600 JSON, values base64 (obfuscation, not encryption) | `<state>/secrets/foreign-values.json` or `AXIOM_FILE_SECRETS_PATH` | **In scope.** (Also the reason a backup matters on hosts without a keychain.) |
| Remote secret managers (OpenBao, AWS / Azure / GCP, Kubernetes) and the `env` provider | The harness holds a reference; a system with its own durability holds the value | Elsewhere | **Out of scope for values.** Metadata, owner and a recovery hint (where to re-issue) are recorded; no value is read |
| Node identity private key (`<state>/identity/private.pem`) | Federation identity and attestation signing key; unrecoverable, a node without it is a different node | A 0600 file | **In scope** (name `file:identity/private.pem`) |
| Audit-chain HMAC delivery artifact (`~/.config/axiom/audit-hmac-key.env`) | Key material for the HMAC-chained audit logs | A file in the user's home | **In scope** (name `file:~/.config/axiom/audit-hmac-key.env`) when present |
| Anything else a deployment keeps | e.g. a service token file | Operator's choice | Opt in with `vault.backup.extra_files` (state-relative, `~`-relative or absolute paths) |
| Capability tokens (vault extension) | Short-lived, re-issuable | Database | Out of scope: expire on their own and are re-issued |
| Externally owned credentials the harness merely holds (`disposition = externally_owned`) | Someone else rotates them | Same local backend | In scope for the value (losing it means asking the owner to re-issue); owner and issuer are in the manifest |

The attestation signing path uses the node identity key above, so it is covered
by the same entry. Env-var and process-environment credentials are discovered
(`secrets discover`) but not held, so there is nothing to back up.

State snapshots keep their own rule set: `identity/**` is still captured there
(`spec-state-snapshots.md`), in plaintext, inside an owner-only directory. A
vault backup does not remove that. See Open questions.

## Guarantees and non-guarantees

* **Confidentiality.** Without the recovery code (or passphrase) an archive
  yields names, fingerprints, sizes and timestamps, and nothing else.
* **Integrity.** Any change to the header, the manifest, the nonce, the wrapped
  key or the ciphertext fails authentication. The ledger and a head record kept
  outside the backup directory make deletion, substitution and rollback visible.
* **Honesty about automation.** A run with no person present can only encrypt
  to a public key. With none configured it says so and **writes nothing**; it
  never falls back to plaintext.
* **Not guaranteed.** Protection against someone who holds an archive AND the
  recovery code. Secrecy of credential *names*, value *lengths* or the one-way
  *fingerprint* (a truncated SHA-256: it lets someone who already holds a
  guess confirm it, which is why it is treated like the existing metadata
  index, not as public). A host compromise while the harness is running (the
  values are readable by the process by design).

## Archive format

One file per backup: `<root>/archives/<id>.axvb`, `id = vb-<UTC time>-<6 hex>`.

```
MAGIC "AXVB\x01\n" | uint32 BE header length | header (canonical JSON, cleartext) | AES-256-GCM ciphertext
```

The header holds: `format`, `id`, `created_at`, `host`, `axiom_version`, `count`,
the per-entry manifest (`name`, `kind`, `scope`, `fingerprint`, `size`,
`metadata` from the index, a `recovery_hint` for metadata-only entries) and the
`enc` block (algorithm, nonce, key-agreement material, wrapped key, KDF
parameters). The ciphertext covers the values. **The entire header is the AEAD
associated data**, so the cleartext manifest is authenticated even though it is
readable.

Encryption (all from `cryptography`, nothing home-rolled):

| Piece | Choice | Why |
|---|---|---|
| Content cipher | AES-256-GCM, 96-bit random nonce, one use per key | Authenticated encryption, already a dependency; a fresh key per archive removes nonce-reuse concern |
| Default mode: recipient | Fresh X25519 ephemeral key per archive; ECDH with the recipient public key; HKDF-SHA256 (salt = ephemeral pub \|\| recipient pub, info `axiom-vault-backup/v1/content`) | **Backup needs no secret.** A scheduled run holds only a public key, so it can encrypt unattended. The same shape age uses |
| Recipient private key | scrypt (N=2^15, r=8, p=1: 32 MiB per guess) derives a key-encryption key from the recovery code or passphrase; AES-256-GCM wraps the 32-byte private key (AAD binds the public key) | Memory-hard KDF from a dependency already present (`argon2` is not installed). The wrapped key is **embedded in every archive**, so one archive plus the code restores on a bare machine |
| Passphrase mode (`backup --passphrase`) | scrypt straight to the content key | A one-off copy for someone with no key set up; interactive only |
| Recovery code | 160 random bits, 32 base32 characters in groups of four plus a 4-character typo check, e.g. `XXXX-…-CCCC`; shown once; never stored | 160 bits of entropy makes the scrypt cost a non-issue and removes the weak-passphrase failure mode |
| User-chosen passphrase | At least 12 characters (NFKC-normalised) wraps the same recipient key | For people who will not keep a printed code. Weaker by construction; the wrapped key is in every archive, so an attacker with an archive can guess offline at scrypt speed |
| Verify-on-write | The writer re-derives the content key from its in-memory ephemeral secret, reads the file back from disk, decrypts, and compares every value | Proves the bytes on disk decrypt, without the recipient private key |
| Failure | Once a file parses, every cryptographic failure (wrong or mistyped code, flipped bit, edited header, short ciphertext) raises one error with one message after the same work: `cannot decrypt: wrong key or damaged backup` | No oracle about which part was wrong. Structural failures (not a backup, truncated, newer format, absurd KDF parameters) are reported separately and never depend on any key |
| Bounds | KDF parameters are range-checked from the header before any work (memory cap 256 MiB); header length capped at 16 MiB | A hostile archive cannot make a restore burn memory |

Files are 0600, directories 0700, written to a sibling temp file, fsynced,
renamed, with the directory fsynced. Before writing, free space of at least
twice the archive plus 1 MiB is required, else nothing is written.

## Key custody

* **Setup, once, with a person present:** `vault key-init`. It generates the
  recipient key, wraps the private half under a freshly minted recovery code and
  shows the code once (stderr, with a retype-the-last-group confirmation), or
  writes it to a **new** 0600 file with `--code-out FILE` for a headless setup.
  `--passphrase` uses a passphrase you choose instead.
* **What is stored where:**

  | Item | Location | Secret? |
  |---|---|---|
  | Public key + wrapped private key | `<state>/secrets/vault-backup-recipient.json` (0600) and inside every archive | No: useless without the code |
  | Recovery code / passphrase | **Nowhere on the machine.** Your offline copy | Yes |
  | Ledger head record | `<state>/secrets/vault-backup-head.json` | No |

* **The code is never a CLI argument or an environment variable.** Restore reads
  it from a hidden prompt or `--code-file FILE` (warns if the file is readable
  by others). It is excluded from `repr`, results, logs and the audit trail;
  tests assert this with canary strings.
* **Replacing the key** (`key-init --force`) affects new backups only. Older
  archives keep needing the old code. `key-info` reports how many archives
  each key fingerprint covers.
* **Losing the code means losing the backups.** There is no recovery path by
  design. `key-info` and `verify --deep` exist to find that out on a calm day.

## When and where

| Trigger | Behaviour |
|---|---|
| On demand | `vault backup [--to DIR] [--force] [--passphrase]` |
| After `secrets set` / `rm` / rotate / `exposed` | `note_change`: no-op unless a key is configured; the first change backs up immediately, later changes within `debounce_seconds` (300) only mark a pending flag the scheduled tick picks up. Never prompts, never raises: a backup problem cannot fail the command that changed the vault. When no key exists the command's output says so in one line |
| Schedule | `vault.backup_tick`, an interval cadence (30 minutes) in the extension manifest; the skill applies `interval_hours` (24) and the pending flag. With no recipient it returns a failure that says the scheduled path cannot encrypt without a key and that nothing was written |
| Before a restore | Automatic when the vault is not empty and the restore would change something |
| Skip | Unchanged inputs (a digest of kind, name, fingerprint and metadata, excluding `updated_at` / `last_verified_at`) produce no new archive unless `--force` |

**Locations.** Default `<state>/vault-backups/` (excluded from state snapshots
by rule). `--to DIR` writes elsewhere (an external disk). `vault.backup.second_copy`
mirrors the archives and the ledger to another directory after every change; a
failing mirror is a warning, never a loss of the primary. Every report warns
when the backups are on the same device as the state.

## Retention

Reuses `plan_retention` and `RetentionPolicy` from the snapshot engine with a
vault-specific default. Archives are tiny, so there is no storage argument for
thinning early; every archive is a copy of live credentials, so there is a risk
argument for not keeping dense history long.

| Key | Snapshots | Vault backup | Reason |
|---|---|---|---|
| `keep_recent_hours` | 24 | **6** | Shorter high-frequency window |
| `keep_last` | 24 | **10** | |
| `keep_daily` / `keep_weekly` | 14 / 8 | 14 / 8 | |
| `keep_monthly` | 6 | **24** | The realistic reason to open an old archive is "the credential I had last year" |
| `protect_newest` | 3 | 3 | |
| `max_count` / `max_bytes` | 100 / 2 GiB | 60 / 256 MiB | |

The snapshot invariants carry over: the newest `protect_newest` are never
pruned, pinned ones are never pruned, the newest verified one is never pruned,
and if none verifies nothing is pruned, so the last backup cannot be deleted.
`vault prune` is a dry run until `--apply`.

## Restore

`vault restore <id|latest> [--only NAME...] [--apply] [--overwrite] [--include-tainted] [--skip-pre-backup] [--force]`

1. Resolve the id. `latest` is the newest backup that verifies, **opens with the code you gave**, holds
   no exposed value, and was not itself made by a restore. Newer ones that fail are passed over and named
   (`FELL BACK: skipped vault backup <id>: <reason>`); if none can be used the restore is refused with one
   line per backup. If the primary directory has no usable backup and `vault.backup.second_copy` does, the
   second copy is used and the report says `USED THE SECOND COPY`. `latest` is refused when the head
   recorded outside the directory shows it was rolled back or emptied (`--force` overrides; naming an id
   restores with a warning).
2. Decrypt with the recovery code, then build the plan against the live vault.
   Each entry is `add`, `unchanged`, `conflict` (same name, different
   fingerprint) or `withheld` (exposed). A dry run, the default, prints names
   and fingerprints and stops.
3. `--apply` first takes an encrypted **pre-restore backup** of the current
   vault. If the vault is not empty and no key exists, or the backup cannot be made (disk full,
   permissions, lock held), it refuses before changing anything; `--skip-pre-backup` proceeds with no undo.
4. Writes go through the owning API: `ForeignCredentialStore.set` for
   credentials (index, fingerprint and custody stay consistent) and an atomic
   0600 write for key files. **Conflicts are skipped** unless `--overwrite`.
5. Every written entry is read back and its fingerprint compared; any mismatch
   fails the command. An entry that cannot be written is listed (`NOT restored`, with the OS reason) and
   the rest still are; re-running writes only what still differs.
6. Names only are written to the ledger and to the action audit
   (`vault.backup.restore`).

Not restorable from here: metadata-only entries (custody is elsewhere). They are
listed with their recovery hint.

Restoring a backup does not recreate `created_at` on the index row (the store
API sets it on first write).

## Exposure and rotation

An archive holds the value as it was when written. After `secrets exposed`, every
older archive still holds the leaked value.

* `secrets exposed` records the exposure, then calls `taint` **before**
  rotating, while the index still describes the exposed value. A taint is
  `(name, fingerprint)` in the ledger. It needs no key and reads no value.
* A backup is *tainted* for a name when its manifest holds that name with that
  fingerprint. Tainted backups leave `latest`; `restore` withholds the exposed
  value; `ls` and `show` mark it. A backup written after rotation holds a new
  fingerprint and is clean.
* `vault scrub --name N` with the recovery code **re-encrypts every affected
  archive without that value** (to the same recipient, verified in memory,
  swapped in atomically, old and new hashes in the ledger). Without a code it
  only reports the tainted set. `--all-versions` removes every stored version.
* `restore --include-tainted` exists for the case where an exposed value must
  be restored anyway; it prints a loud warning to rotate again.

A scrub rewrites archives in place and cannot reach copies made elsewhere (a
second disk that was not mounted, a cloud sync). Treat a leaked credential as
leaked; scrub reduces where its old value lives, it does not undo the exposure.

## Emergency path

`scripts/axi-restore.py` (its `vault` subcommands) imports nothing from Axiom. It needs Python 3.11+,
the `cryptography` package, one `.axvb` file and the recovery code (hidden prompt
or `--code-file`).

```
python3 -I axi-restore.py vault list    PATH...           # names and fingerprints, no key
python3 -I axi-restore.py vault verify  PATH
python3 -I axi-restore.py vault extract PATH --to NEWDIR [--only GLOB]   # 0600 files in a 0700 dir
python3 -I axi-restore.py vault reveal  ARCHIVE NAME      # one value to stdout
```

`PATH` is one archive or a backup directory; given a directory it opens the newest archive that
decrypts with your code and lists every newer one it skipped and why. It does not write into the vault;
put values back with the CLI once it works.
It is tested under `python -I` with a shadow `axiom` package that raises on
import.

## Contracts

### CLI (`axi vault ...`, names via the brand helper)

| Verb | Behaviour |
|---|---|
| `backup [--to DIR] [--force] [--passphrase] [--reason R]` | Back up now. Skips when unchanged. |
| `ls [--from DIR]` | Backups: id, time, count, reason, flags (`PINNED`, `TAINTED`, damage). |
| `show [ID]` | Manifest: names, scope, fingerprints, exposed marks. No key. |
| `verify [ID] [--deep] [--code-file F]` | Ledger chain, rollback evidence, file hashes, headers, unrecorded files. `--deep` decrypts and compares every fingerprint. |
| `restore [ID] [--only N...] [--apply] [--overwrite] [--include-tainted]` | See Restore. Dry run unless `--apply`. |
| `prune [--apply]` / `pin ID` / `unpin ID` | Retention. |
| `scrub --name N [--all-versions] [--code-file F]` | See Exposure. |
| `key-info` | Whether a key exists, its fingerprint, which archives it covers, custody reminders. Never the key. |
| `key-init [--passphrase] [--code-out FILE] [--force]` | Create the key; show the recovery code once. |

Each verb is a skill (`vault.backup`, `vault.backup_ls`, `vault.backup_show`,
`vault.backup_verify`, `vault.backup_restore`, `vault.backup_prune`,
`vault.backup_pin`, `vault.backup_unpin`, `vault.backup_scrub`, `vault.key_info`,
`vault.key_init`, `vault.backup_tick`) registered through `SkillRegistry`; the CLI
dispatches through `invoke_capability`. All are CLI-only (none is projected to
MCP: they handle the material the vault exists to protect). `restore`, `scrub`,
`prune` and `key_init` are journaled to the universal action audit. Every output
ends with real next-step commands.

### Settings (`vault.backup.*`, global scope in `settings.toml`)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | true | Master switch for scheduled and automatic backups |
| `interval_hours` | 24 | Scheduled cadence |
| `debounce_seconds` | 300 | After a change, do not back up again sooner than this |
| `auto_prune` | true | Run retention after each scheduled backup |
| `second_copy` | `""` | A second directory to mirror to |
| `extra_files` | `[]` | More key files to include |
| `keep_*`, `protect_newest`, `max_count`, `max_bytes` | see Retention | Retention |

### Python seams

```python
from axiom.infra.vault_backup import (
    create_backup, restore, scrub, verify, prune, list_backups, show,   # engine
    init_key, key_info, tick, note_change, taint_exposed,               # custody and triggers
)
```

## Behaviour under damage

| Situation | Result |
|---|---|
| Archive truncated, zeroed, bit-flipped, replaced | `verify` reports it without a key (hash vs ledger); it drops out of `latest`; restore refuses |
| Archive deleted | `verify` reports it missing; the rest are unaffected |
| Archive planted in the directory but not in the ledger | Reported as unrecorded; never selected |
| Ledger edited | The chain breaks at the first altered entry |
| Backup directory replaced with an older copy | `verify` reports `ROLLBACK SUSPECTED` (the head recorded outside the directory is ahead) |
| Wrong or mistyped code | One generic error; nothing changes |
| Archive from a newer format | Refused with a message naming the version and the fix (upgrade, or use the newer script) |
| Disk full or I/O error mid-write | The new archive is not published; existing archives and the ledger are byte-identical; the temp file is removed |
| Killed mid-write | Only a `.tmp-*` file remains; it is ignored and swept by `prune` |
| Directory renamed or missing | `ls` is empty, a new store is created on the next backup, the renamed one still verifies |
| Two backups at once | An advisory lock serialises them; the second is `unchanged` or waits up to 10 s, then reports busy |
| Second copy unavailable | Warning; primary intact |

## Failure handling contract

What `vault restore` does when something is wrong, and what the operator does. Each row is a drill in
`tests/infra/backup_drills/` (vault backup x scenario, plus the same failures against the emergency
script), judged against the vault's actual fingerprints before and after, and each lands in one outcome
class: **restored**, **restored-from-fallback**, **partial-restore-reported** or **refused-safely**. A
crash, a hang, a torn write, silently wrong data, or any secret, key or recovery code in the output fails
the drill (canary values are searched for raw and base64 in everything printed and logged).

| Situation | Result | Operator |
|---|---|---|
| Newest archive damaged (bit flip, truncated, zero-length, header edited), written by a newer format, or made with a different key | `restore latest` uses the next newest that verifies and opens; prints which were skipped and why | Nothing; `vault verify`, then take a fresh backup |
| Ledger line for the newest backup broken | Backups recorded before the break are still trusted; those at or after it are passed over | Restore the ledger from the second copy |
| Every archive bad or unopenable | Refused, one line per archive, then second copy / emergency script / `--from DIR` | `python3 -I axi-restore.py vault verify DIR --code-file F` |
| Backup dir missing, renamed, replaced by a file, unreadable, unmounted | Refused naming which; if `second_copy` is configured it is used automatically and the report says so | Mount it back or use the second copy |
| Ledger deleted or rolled back | `LEDGER MISSING` / `ROLLBACK SUSPECTED` from the head kept in `secrets/vault-backup-head.json`; archives the ledger does not record are never selected; `latest` refused | Restore the ledger from the second copy, or open the archives with the emergency script. The recorded head is never moved backwards, so new backups cannot launder a rollback |
| Wrong or lost recovery code / passphrase | One generic error, identical for a wrong code and a damaged archive (no oracle), followed by the real options: re-enter the saved code, `key-info`, a second copy, and what a lost code means (re-issue, `key-init --force`, new backup) | Find the code; otherwise re-issue credentials |
| Restore killed part-way | Each entry is old or new, never torn; re-run writes only what differs; the pre-restore backup undoes | Re-run |
| Conflicting value already present | Left alone and listed under **PARTIAL RESTORE** ... **LEFT ALONE**; `--overwrite` replaces it after a safety backup | Decide per name |
| A symlink or read-only directory at a key-file path | That entry is listed as not restored (a symlink's target is never written); the rest are restored | Fix the path, re-run |
| Disk full or I/O error while restoring | Written entries stay, the rest are listed with the reason | Free space, re-run |
| Disk full while backing up | `DiskPressureError`; existing archives and ledger byte-identical | Free space or `--to` another disk |
| Safety backup cannot be made | Restore refuses, nothing changed | Fix the cause, or `--skip-pre-backup` |
| A write that did not land | Caught by the read-back; reported `re-verification failed for NAME`, exit 1 | Re-run |
| Another backup/restore holds the lock | Refused naming the lock | Retry |
| Clock wrong | `latest` follows ledger order, not timestamps; a future-dated newest backup does not stop the scheduled one | Fix the clock |
| Fresh machine | Works from the archives and the code alone (no recipient record, no ledger knowledge needed with the emergency script) | `axi-restore.py vault extract DIR --to NEW` |
| Retention asked to keep nothing | `protect_newest` floors at 1; the newest verified archive is never pruned | Fix the settings |

The drills themselves, and how to run them, are described under "Restore drills" in
`spec-state-snapshots.md`; the quarterly drill below is the human version.

## Runbooks

### Custody (do this once, then yearly)

1. `axi vault key-init` in a terminal. Write the recovery code on paper or print
   it. Do not photograph it, paste it into chat, or put it in a password manager
   on this same machine.
2. Keep one copy offline and away from the machine (a drawer, a safe). Keep a
   second copy with a second trusted person if the credentials outlive you.
   Someone holding the code AND an archive can read every credential in it.
3. `axi vault backup --to /Volumes/<external>/vault` once, or set
   `vault.backup.second_copy`, so a disk loss does not take the backups with it.
4. `axi vault key-info` to confirm the fingerprint and which disk the backups are on.
5. Yearly, run the drill below.
6. If the code may have been seen, run `axi vault key-init --force`, take a new
   backup, and then treat every credential in the old archives as exposed
   (`axi vault exposed NAME --where <where>` rotates them and taints the old
   archives; `axi vault scrub --name NAME` with the OLD code purges them).

### Restore drill (quarterly, 10 minutes, changes nothing real)

1. `axi vault verify --deep` and type the code. It must say `ok` for every archive.
   A failure here is the whole point of the drill: investigate now.
2. `axi vault show latest` and check the names against `axi vault list`.
3. On a scratch directory, with Axiom out of the picture:
   `python3 -I scripts/axi-restore.py vault extract <archive> --to /tmp/drill --code-file <file>`,
   compare a fingerprint, then delete `/tmp/drill`.
4. `axi vault restore latest` (dry run). Every entry should read `unchanged`.
5. Record the date. Do not run `--apply` in a drill.

### Real recovery

| Situation | Do this |
|---|---|
| One credential is wrong or missing | `axi vault restore latest --only NAME` then add `--apply` (add `--overwrite` to replace a different value) |
| New machine, nothing installed | Install Python 3.11+ and `cryptography`; `python3 -I axi-restore.py vault extract <archive-or-dir> --to <newdir>`; install Axiom; `axi vault set NAME` for each; delete the directory |
| New machine with Axiom installed, whole backup directory available | Copy the directory (`archives/` and `ledger.jsonl`) anywhere, then `axi vault restore latest --from <dir> --code-file F` (dry run), then add `--apply` |
| Only loose archive files survived (no ledger) | The CLI lists backups from the ledger and never trusts an unrecorded archive, so use the emergency script on the archive or its directory, then `axi vault set` |
| The CLI will not start | The emergency script, as above |
| A restore was the wrong call | The output named a pre-restore backup: `axi vault restore <that id> --apply --overwrite` |
| The recovery code is lost | The backups cannot be opened. Re-issue each credential at its issuer, run `key-init --force`, take a fresh backup |

## Decisions

* ADR-056: every verb is a skill; the CLI only dispatches. The noun stays `vault` (the extension that owns credential custody).
* Recipient (public-key) encryption was chosen over a stored passphrase or an OS-keychain-held key because the unattended path must not hold anything that opens the archives. A key held in the keychain would sit on the machine whose loss the backup insures against.
* The wrapped private key travels inside each archive, so an archive is self-sufficient with the code alone. The cost is that a weak user-chosen passphrase is attackable offline; the generated code is not.
* The manifest is cleartext (authenticated) so `ls`, `show`, taint and the rollback check work with no key. The cost is that names, lengths and fingerprints are visible to whoever holds an archive.

## Open questions

* `identity/**` (including `private.pem`) is still captured in plaintext by state snapshots. Now that the vault backup covers that key, consider excluding it from snapshots, or encrypting it there. Owner: platform.
* Remote targets for `--to` (object storage, another host): a mounted directory only today. Owner: platform.
* Multiple recipients (a second person's public key) so a team can hold independent recovery codes. The format has room (`enc.mode`); the engine does not yet. Owner: platform.
* A signed ledger head (as in the snapshot spec) so a rewrite of the whole directory and the state-dir head record together is detectable. Owner: platform, with the attest work.
* Backing up the database-held vault state (capability-token records) is out of scope here and belongs to the database backup path.

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
