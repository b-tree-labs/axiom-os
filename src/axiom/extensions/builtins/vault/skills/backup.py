# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""KEEP's vault backup skills (ADR-056: one skill function per CLI verb).

``axi vault backup | ls | show | verify | restore | prune | scrub | key-info | key-init``.

These are thin wrappers over :mod:`axiom.infra.vault_backup`.  Contract, enforced by tests with
canary secrets: a result, an action line, an error or a log record produced here names
credentials and fingerprints and NEVER contains a value, a recovery code or a passphrase.

Key material comes in as ``unlock_secret`` (a string or a zero-arg callable that fetches it
from a hidden prompt or a file) and the recovery code goes out through ``reveal`` (a callable the
CLI points at the terminal or a 0600 file).  Neither is ever placed in a result.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import Any

from axiom.infra import state_snapshots as ss
from axiom.infra import vault_backup as vb
from axiom.infra.skills import SkillContext, SkillResult
from axiom.infra.vault_backup import crypto as C
from axiom.infra.vault_backup.policy import default_root

log = logging.getLogger(__name__)


def _cli() -> str:
    return ss._brand_cli()


def _root(params: dict[str, Any], ctx: SkillContext) -> Path:
    arg = params.get("to") or params.get("from_dir") or params.get("root")
    return Path(arg).expanduser() if arg else default_root(Path(ctx.state_dir))


def _unlock(params: dict[str, Any]):
    return params.get("unlock_secret")


def _need_unlock(what: str) -> SkillResult:
    c = _cli()
    return SkillResult(ok=False, errors=[
        f"{what} needs the recovery code (or passphrase). Run it in a terminal to be asked, or pass "
        f"`--code-file PATH` (a file only you can read). If you have lost it, see "
        f"`{c} vault key-info` for what that means."
    ])


def _fail(exc: Exception, **extra: Any) -> SkillResult:
    # Only the message of our own typed errors: they are written never to carry values.
    if isinstance(exc, C.VaultBackupError):
        msg = str(exc)
    elif isinstance(exc, OSError) and exc.errno is not None:
        # An operating-system error carries only the reason and a path, never a value, so it is safe to
        # show, and "PermissionError" alone tells the person nothing they can act on.
        where = f": {exc.filename}" if exc.filename else ""
        reason = exc.strerror or type(exc).__name__
        msg = f"{reason[:1].lower()}{reason[1:]}{where}"  # lowercase-leading, like every other message here
        if isinstance(exc, PermissionError):
            msg += (
                ". Fix: give this user read and write access to that location, or use a different one "
                "(--from DIR for a restore, --to DIR for a backup)"
            )
    else:
        msg = f"{type(exc).__name__}"
    return SkillResult(ok=False, errors=[msg], **extra)


def _safe(fn):
    """Skill boundary: typed backup errors keep their (value-free) message; anything unexpected is
    reduced to its type name, because a backend's exception text is not ours to vouch for."""

    @functools.wraps(fn)
    def inner(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
        try:
            return fn(params, ctx)
        except Exception as exc:  # noqa: BLE001
            log.error("%s failed (%s)", fn.__name__, type(exc).__name__)
            return _fail(exc)

    return inner


def _audit(ctx: SkillContext, op: str, candidate: str, names: list[str], outcome: str) -> None:
    """Names-only provenance record, as ``secrets rm`` does."""
    try:
        from axiom.policy.action_ledger import ActionLedger

        ActionLedger(state_dir=ctx.state_dir).record_action(
            agent="keep", op_class=op, name=op.rsplit(".", 1)[-1], candidate=candidate,
            guards=["human_or_authorized_caller"], outcome=outcome,
            metadata={"surface": "cli", "names": names},
        )
    except Exception:  # noqa: BLE001 -- journaling never blocks the report
        ctx.logger.debug("action-ledger emission failed for %s", op, exc_info=True)


# ---------------------------------------------------------------------------
# Chaining preconditions (ADR-185). Cheap, side-effect free, and they never open a key or a value.


def key_configured(params: dict[str, Any], ctx: SkillContext) -> tuple[bool, str]:
    """A backup key is configured, or the call names a passphrase to use instead. Reads one small file."""
    if params.get("passphrase"):
        return True, ""
    try:
        from axiom.infra.skills import ensure_context

        rec = vb.load_recipient(Path(ensure_context(ctx).state_dir))
    except Exception:  # noqa: BLE001 - an unreadable key file is an unmet precondition, not a crash
        return False, "the backup key record could not be read"
    if rec is not None:
        return True, ""
    return False, (
        "no backup key is configured, so a backup cannot be encrypted without a person present. "
        "Nothing was written."
    )


def recovery_code_given(params: dict[str, Any], ctx: SkillContext) -> tuple[bool, str]:
    """The call carries the recovery code (or passphrase), or a way to ask for it.

    Not "a key is configured on this machine": a restore onto a rebuilt machine has only the archives and
    the code, and that is a supported case. Nothing is opened here; only whether one was supplied.
    """
    given = params.get("unlock_secret") is not None
    return given, "" if given else "restore needs the recovery code (or passphrase), and none was supplied"


def backup_exists(params: dict[str, Any], ctx: SkillContext) -> tuple[bool, str]:
    """An explicitly named backup is in the store. Needs no key.

    ``latest`` (or no id) is not checked here: which backup that means, and why none qualifies, is
    something only the verb itself can say (it lists every backup and the reason each was passed over),
    and a refusal that replaced that with a generic one would be a worse message at the worst moment.
    """
    ref = params.get("id")
    if not ref or ref == "latest":
        return True, ""
    try:
        from axiom.infra.skills import ensure_context

        vb.engine.resolve_id(_root(params, ensure_context(ctx)), ref)
    except C.VaultBackupError as exc:
        return False, str(exc).splitlines()[0]
    except Exception:  # noqa: BLE001
        return False, "the backup store could not be read"
    return True, ""


def credential_named(params: dict[str, Any], ctx: SkillContext) -> tuple[bool, str]:
    name = str(params.get("name") or "").strip()
    return bool(name), "" if name else "no credential name was given"


def somewhere_to_show_the_code(params: dict[str, Any], ctx: SkillContext) -> tuple[bool, str]:
    ok = params.get("reveal") is not None or bool(params.get("passphrase"))
    return ok, "" if ok else "there is nowhere to show the recovery code once, and no passphrase was chosen"


def _row(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r[k] for k in ("id", "created_at", "count", "size", "reason", "status", "pinned", "tainted", "scrubbed")}


# ---------------------------------------------------------------------------


@_safe
def backup(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    c, state = _cli(), Path(ctx.state_dir)
    root = _root(params, ctx)
    try:
        pw = _unlock(params) if params.get("passphrase") else None
        if params.get("passphrase") and pw is None:
            return SkillResult(ok=False, errors=["--passphrase needs a terminal to ask in; nothing was written"])
        res = vb.create_backup(state, root=root, reason=params.get("reason") or "manual",
                               trigger="manual", passphrase=pw, force=bool(params.get("force")))
    except vb.NotConfigured as exc:
        return SkillResult(ok=False, errors=[str(exc)], value={"next_steps": [f"{c} vault key-init"]})
    except C.VaultBackupError as exc:
        return _fail(exc)
    nxt = [f"{c} vault verify", f"{c} vault ls", f"{c} vault restore latest   # dry run first"]
    if root.resolve() == default_root(state).resolve() and ss.same_device(
            root if root.exists() else root.parent, state):
        nxt.insert(0, f"{c} vault backup --to /path/on/another/disk   # these are on the same disk as your state")
    acts: list[str] = []
    if res.status == "created":
        acts.append(f"backed up {res.count} credential value(s) into {res.id} (encrypted, {res.size} bytes)")
    elif res.status == "unchanged":
        acts.append(f"nothing changed since {res.id}; no new backup written (use --force to write one anyway)")
    else:
        acts.append("nothing to back up: the vault holds no values in scope")
    if res.metadata_only:
        acts.append("recorded metadata only (custody is outside the harness): " + ", ".join(res.metadata_only))
    for s in res.skipped:
        acts.append(f"NOT BACKED UP: {s['name']} ({s['reason']})")
    acts += res.warnings
    return SkillResult(
        ok=not res.skipped, actions_taken=acts,
        value={"status": res.status, "id": res.id, "count": res.count, "names": res.names, "path": res.path,
               "skipped": res.skipped, "warnings": res.warnings, "next_steps": nxt},
    )


@_safe
def ls(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    c, root = _cli(), _root(params, ctx)
    rows = [_row(r) for r in vb.list_backups(root)]
    nxt = [f"{c} vault show latest", f"{c} vault verify"] if rows else [f"{c} vault backup", f"{c} vault key-info"]
    return SkillResult(ok=True, value={"root": str(root), "backups": rows, "next_steps": nxt},
                       actions_taken=[f"{len(rows)} backup(s) in {root}"])


@_safe
def show(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    c = _cli()
    try:
        info = vb.show(_root(params, ctx), params.get("id") or "latest")
    except C.VaultBackupError as exc:
        return _fail(exc)
    return SkillResult(ok=True, value={**info, "next_steps": [
        f"{c} vault restore {info['id']}   # dry run", f"{c} vault verify {info['id']}"]},
        actions_taken=[f"{info['id']}: {info['count']} entr(ies), written {info['created_at']}"])


@_safe
def verify(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    c, root = _cli(), _root(params, ctx)
    unlock = _unlock(params) if params.get("deep") else None
    if params.get("deep") and unlock is None:
        return _need_unlock("deep verification")
    try:
        rep = vb.verify(Path(ctx.state_dir), root, ref=params.get("id"), unlock=unlock)
    except C.VaultBackupError as exc:
        return _fail(exc)
    acts = [f"checked {rep.checked} backup(s) ({'decrypted and compared' if rep.deep else 'ledger, hashes, headers; no key used'})"]
    if rep.stray_temp:
        acts.append(f"{len(rep.stray_temp)} leftover temp file(s) from an interrupted write; `{c} vault prune --apply` clears them")
    nxt = [f"{c} vault verify --deep   # decrypts; proves the recovery code still opens them"] if not rep.deep \
        else [f"{c} vault ls"]
    if not rep.ok:
        nxt = [f"{c} vault backup --force   # make a fresh one now", f"{c} vault ls"]
    return SkillResult(ok=rep.ok, errors=rep.problems, actions_taken=acts,
                       value={"ok": rep.ok, "checked": rep.checked, "deep": rep.deep, "orphans": rep.orphans,
                              "next_steps": nxt})


@_safe
def restore(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    c, state, root = _cli(), Path(ctx.state_dir), _root(params, ctx)
    unlock = _unlock(params)
    if unlock is None:
        return _need_unlock("restore")
    apply = bool(params.get("apply"))
    ref = params.get("id") or "latest"
    try:
        rep = vb.restore(state, root, ref, unlock=unlock, only=tuple(params.get("only") or ()),
                         apply=apply, overwrite=bool(params.get("overwrite")),
                         include_tainted=bool(params.get("include_tainted")),
                         skip_pre_backup=bool(params.get("skip_pre_backup")),
                         force=bool(params.get("force")))
    except C.DecryptionError as exc:
        return SkillResult(ok=False, errors=[str(exc), vb.engine.DECRYPT_HELP.format(cli=c)],
                           value={"next_steps": [f"{c} vault key-info", f"{c} vault ls --from DIR"]})
    except vb.NoSuchBackup as exc:  # includes NoUsableBackup: the message says why, line by line
        return SkillResult(ok=False, errors=str(exc).splitlines(),
                           value={"next_steps": [f"{c} vault ls", f"{c} vault verify", f"{c} vault restore latest --from DIR"]})
    except vb.PreRestoreFailed as exc:
        return SkillResult(ok=False, errors=[str(exc)],
                           value={"next_steps": [f"{c} vault restore {ref} --apply --skip-pre-backup   # no safety net"]})
    acts = [f"backup {rep.id}: {'RESTORED' if apply else 'DRY RUN, nothing changed'}"]
    acts += rep.notes
    for i in rep.items:
        fp = (i.backup_fingerprint or "")[:12]
        now = f" (vault now: {i.current_fingerprint[:12]})" if i.current_fingerprint else ""
        acts.append(f"  {i.action:10} {i.name}  backup={fp}{now}" + (f"  -- {i.note}" if i.note else ""))
    if rep.metadata_only:
        acts.append("not restorable here (custody is outside the harness; re-issue instead): " + ", ".join(rep.metadata_only))
    acts += rep.warnings
    if rep.pre_restore_id:
        acts.append(f"before changing anything, the current vault was backed up as {rep.pre_restore_id}")
    if apply:
        _audit(ctx, "vault.backup.restore", rep.id, rep.written, "proceeded" if not (rep.failed_verification or rep.not_written) else "failed")
    if apply and rep.skipped_conflicts:
        acts.append(
            f"PARTIAL RESTORE: {len(rep.written)} restored ({', '.join(rep.written) or 'none'}); "
            f"{len(rep.skipped_conflicts)} LEFT ALONE because a different value is in the vault now "
            f"({', '.join(rep.skipped_conflicts)}). --overwrite replaces them (a safety backup is taken first).")
    errors = [f"re-verification failed for {n}" for n in rep.failed_verification]
    if rep.not_written:
        errors += [f"{n}: NOT restored ({why})" for n, why in rep.not_written.items()]
        errors.append(
            f"PARTIAL RESTORE: {len(rep.written)} restored ({', '.join(rep.written) or 'none'}), "
            f"{len(rep.not_written)} not ({', '.join(rep.not_written)}). Re-run the same command after fixing the "
            "cause: it only writes what still differs.")
    nxt: list[str] = []
    if not apply:
        has_conflict = any(i.action == "conflict" for i in rep.items)
        nxt.append(f"{c} vault restore {rep.id} --apply" + ("   # conflicts are skipped" if has_conflict else ""))
        if has_conflict:
            nxt.append(f"{c} vault restore {rep.id} --apply --overwrite   # replaces the conflicting values")
    else:
        nxt.append(f"{c} vault audit")
        if rep.skipped_conflicts:
            nxt.append(f"{c} vault restore {rep.id} --apply --overwrite --only <name>   # to take the backup's value")
        if rep.pre_restore_id:
            nxt.append(f"{c} vault restore {rep.pre_restore_id} --apply --overwrite   # undo this restore")
        if rep.not_written or rep.failed_verification:
            nxt.insert(0, f"{c} vault restore {rep.id} --apply   # re-run: only what still differs is written")
    return SkillResult(
        ok=not (rep.failed_verification or rep.not_written), errors=errors,
        actions_taken=acts,
        value={"id": rep.id, "applied": rep.applied, "written": rep.written, "withheld": rep.withheld,
               "skipped_conflicts": rep.skipped_conflicts, "pre_restore_id": rep.pre_restore_id,
               "not_written": rep.not_written, "skipped_backups": rep.skipped_backups,
               "items": [{"name": i.name, "action": i.action, "backup_fingerprint": i.backup_fingerprint,
                          "current_fingerprint": i.current_fingerprint} for i in rep.items],
               "next_steps": nxt},
    )


@_safe
def prune(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    c, state, root = _cli(), Path(ctx.state_dir), _root(params, ctx)
    apply = bool(params.get("apply"))
    try:
        out = vb.prune(state, root, apply=apply)
    except C.VaultBackupError as exc:
        return _fail(exc)
    p = out.plan
    return SkillResult(
        ok=True,
        value={"applied": out.applied, "keep": [{"id": d.id, "reasons": list(d.reasons)} for d in p.keep],
               "delete": [{"id": d.id, "reasons": list(d.reasons)} for d in p.delete],
               "warnings": list(p.warnings),
               "next_steps": [f"{c} vault prune --apply"] if p.delete and not apply else [f"{c} vault ls"]},
        actions_taken=[f"{'pruned' if apply else 'would prune'} {len(p.delete)} backup(s); keeping {len(p.keep)}"],
    )


@_safe
def pin(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    return _pin(params, ctx, True)


@_safe
def unpin(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    return _pin(params, ctx, False)


def _pin(params: dict[str, Any], ctx: SkillContext, pinned: bool) -> SkillResult:
    if not params.get("id"):
        return SkillResult(ok=False, errors=["needs a backup id"])
    try:
        bid = vb.engine.set_pinned(_root(params, ctx), params["id"], pinned, Path(ctx.state_dir))
    except C.VaultBackupError as exc:
        return _fail(exc)
    return SkillResult(ok=True, value={"id": bid, "pinned": pinned, "next_steps": [f"{_cli()} vault ls"]},
                       actions_taken=[f"{'pinned' if pinned else 'unpinned'} {bid}"])


@_safe
def scrub(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    c, state, root = _cli(), Path(ctx.state_dir), _root(params, ctx)
    name = params.get("name")
    if not name:
        return SkillResult(ok=False, errors=["scrub needs --name <credential>"])
    unlock = _unlock(params)
    try:
        out = vb.scrub(state, root, name, unlock=unlock, all_versions=bool(params.get("all_versions")))
    except C.VaultBackupError as exc:
        return _fail(exc)
    acts = [f"{len(out.tainted_ids)} backup(s) hold an exposed value of {name!r}"]
    nxt: list[str] = []
    if unlock is None and out.tainted_ids:
        acts.append("they are excluded from `latest` and the exposed value is withheld on restore, but it is "
                    "still inside those archives")
        nxt.append(f"{c} vault scrub --name {name}   # in a terminal: asks for the recovery code and purges it")
    elif not out.tainted_ids:
        acts.append(f"nothing tainted for {name!r}; use --all-versions to remove every stored version of it")
    if out.rewritten:
        acts.append(f"re-encrypted {len(out.rewritten)} backup(s) without {name!r}: " + ", ".join(out.rewritten))
        _audit(ctx, "vault.backup.scrub", name, [name], "proceeded")
    for bid in out.failed:
        acts.append(f"COULD NOT scrub {bid} (wrong code, or damaged); it is untouched")
    nxt.append(f"{c} vault verify --deep")
    return SkillResult(ok=not out.failed, errors=[f"could not scrub {b}" for b in out.failed], actions_taken=acts,
                       value={"name": name, "tainted": out.tainted_ids, "rewritten": out.rewritten,
                              "failed": out.failed, "purged": out.purged, "next_steps": nxt})


@_safe
def key_info(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    c, state = _cli(), Path(ctx.state_dir)
    info = vb.key_info(state, _root(params, ctx))
    acts: list[str] = []
    nxt: list[str] = []
    if not info["configured"]:
        acts.append("no backup key: scheduled and automatic backups CANNOT run (they will not write plaintext)")
        nxt.append(f"{c} vault key-init")
    else:
        acts.append(f"backups are encrypted to key {info['recipient_fingerprint']} (unlocked by your {info['unlock']})")
        acts.append("the recovery code / passphrase is NOT stored anywhere on this machine; "
                    "without it no backup can be restored")
        if info["same_device_as_state"] and not info["second_copy"]:
            acts.append("backups are on the same disk as your state; keep a copy elsewhere")
            nxt.append(f"{c} vault backup --to /path/on/another/disk")
        nxt.append(f"{c} vault verify --deep   # proves the code you hold still opens the backups")
    return SkillResult(ok=True, value={**info, "next_steps": nxt}, actions_taken=acts)


@_safe
def key_init(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Create the backup key.  The recovery code leaves ONLY through ``reveal``."""
    c, state = _cli(), Path(ctx.state_dir)
    reveal = params.get("reveal")
    pw = _unlock(params) if params.get("passphrase") else None
    if reveal is None and pw is None:
        return SkillResult(ok=False, errors=[
            "key-init needs somewhere to show the recovery code once (a terminal or `--code-out FILE`), or "
            "`--passphrase` to use one you choose. Nothing was created."])
    try:
        ki = vb.init_key(state, passphrase=pw, force=bool(params.get("force")))
    except C.VaultBackupError as exc:
        return _fail(exc)
    if ki.recovery_code and reveal is not None:
        reveal(ki.recovery_code)
    _audit(ctx, "vault.backup.key_init", ki.recipient.fingerprint, [], "proceeded")
    return SkillResult(
        ok=True,
        actions_taken=[f"created backup key {ki.recipient.fingerprint} (unlock: {ki.recipient.label})",
                       "the recovery code was shown once and is not stored; keep a printed or offline copy"],
        value={"fingerprint": ki.recipient.fingerprint, "unlock": ki.recipient.label,
               "next_steps": [f"{c} vault backup", f"{c} vault verify --deep", f"{c} vault key-info"]},
    )


@_safe
def tick(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Scheduled backup.  Never prompts; with no key it says so and writes nothing."""
    t = vb.tick(Path(ctx.state_dir), force=bool(params.get("force")))
    value = {"status": t.status, "pruned": t.pruned}
    if not t.ok:
        log.error("%s", t.message)
        return SkillResult(ok=False, errors=[t.message], value=value)
    return SkillResult(ok=True, actions_taken=[t.message], value=value)
