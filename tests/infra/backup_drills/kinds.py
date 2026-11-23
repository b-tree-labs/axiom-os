# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The three backup kinds, driven the way a person drives them (real CLIs, real stores)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

from axiom.infra import backup as pg
from axiom.infra import backup_core as bc
from axiom.infra import state_snapshots as ss
from tests.infra.backup_core.conftest import CANARY_A, CANARY_B, PASSPHRASE

from . import damage as D
from .harness import DRIVER, SCRIPT, Harness, Result, call, run_script  # noqa: F401

GEN_COUNT = 3


def _tree_digest(state: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(state.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        rel = p.relative_to(state).as_posix()
        if (rel.split("/")[0].startswith(("snapshots", "audit")) or rel == "secrets/snapshot-head.json"
                or ".pre-restore-" in rel or ".restore-" in rel or rel.endswith(".lock")):  # (audit/: churn)
            continue
        out[rel] = bc.sha256_file(p)
    return out


# ===========================================================================
# state snapshots
# ===========================================================================


class StateHarness(Harness):
    kind = "state"
    hows = frozenset({"bitflip", "truncate", "zero", "delete", "manifest-edit", "manifest-zero", "chain-break",
                      "wrong-version"})

    def __init__(self, sandbox: Path, state_dir: Path) -> None:
        super().__init__(sandbox, state_dir)
        self.root = state_dir / "snapshots"
        self.store = ss.open_store(state_dir, self.root)
        self.policy = ss.SnapshotPolicy(auto_prune=False)

    def _write_gen(self, n: int) -> None:
        s = self.state
        (s / "identity").mkdir(exist_ok=True)
        (s / "memory").mkdir(exist_ok=True)
        (s / "sessions").mkdir(exist_ok=True)
        (s / "settings.toml").write_text(f"[gen]\nn = {n}\n")
        (s / "identity" / "node.json").write_text('{"node": "drill"}')
        os.chmod(s / "identity" / "node.json", 0o600)
        (s / "memory" / "ledger.jsonl").write_text(f'{{"gen": {n}}}\n' * n)
        (s / "sessions" / f"s{n}.json").write_text(f'{{"session": {n}}}')

    def make_generations(self, n: int = GEN_COUNT, second_copy: bool = False, **kw: Any) -> None:
        for g in range(1, n + 1):
            self._write_gen(g)
            r = ss.create_snapshot(self.state, self.root, policy=self.policy, force=True, prune=False)
            self.ids.append(r.snapshot_id)
            self.gen_digests[g] = _tree_digest(self.state)
        if second_copy:
            self.second = self.sandbox / "second-disk" / "snapshots"
            self.second.parent.mkdir()
            D.copy_tree(self.root, self.second)

    def break_target(self) -> None:
        (self.state / "settings.toml").write_text("DAMAGED")
        (self.state / "memory" / "ledger.jsonl").unlink()
        shutil.rmtree(self.state / "identity")

    def digest(self) -> dict[str, str]:
        return _tree_digest(self.state)

    def _blob(self, bid: str) -> Path:
        m = json.loads((self.root / "snaps" / bid / "manifest.json").read_text())
        sha = next(f["sha256"] for f in m["body"]["files"] if f["path"] == "settings.toml")
        return self.store.blob_path(sha)

    def damage(self, bid: str, how: str) -> None:
        mf = self.root / "snaps" / bid / "manifest.json"
        if how == "bitflip":
            D.bit_flip(self._blob(bid))
        elif how == "truncate":
            D.truncate(self._blob(bid))
        elif how == "zero":
            D.zero_length(self._blob(bid))
        elif how == "delete":
            self._blob(bid).unlink()
        elif how == "manifest-edit":
            D.edit_json(mf, lambda d: d["body"].__setitem__("reason", "tampered"))
        elif how == "manifest-zero":
            D.zero_length(mf)
        elif how == "chain-break":
            D.edit_json(mf, lambda d: d.__setitem__("prev", "f" * 64))
        elif how == "wrong-version":
            m = json.loads(mf.read_text())
            body = dict(m["body"], version=2)
            mf.write_text(json.dumps(bc.build_manifest(m["prev"], body), indent=1, sort_keys=True))
        else:  # pragma: no cover
            raise ValueError(how)

    def _argv(self, verb: str, ref: str | None = None, *, to: Path | None = None, apply: bool = True,
              force: bool = False, only: tuple[str, ...] = ()) -> list[str]:
        argv = [verb] + ([ref] if ref else [])
        if apply and verb == "restore":
            argv.append("--apply")
        if to:
            argv += ["--to", str(to)]
        if force:
            argv.append("--force")
        for o in only:
            argv += ["--only", o]
        return argv

    def _cli(self, argv: list[str]) -> Result:
        from axiom.extensions.builtins.state import cli as state_cli

        return call(lambda: state_cli.main(argv), capture=self.capture)

    def restore_latest(self, *, to: Path | None = None, force: bool = False, only: tuple[str, ...] = (),
                       apply: bool = True) -> Result:
        return self._cli(self._argv("restore", None, to=to, force=force, only=only, apply=apply))

    def restore_id(self, bid: str, *, to: Path | None = None, force: bool = False,
                   only: tuple[str, ...] = ()) -> Result:
        return self._cli(self._argv("restore", bid, to=to, force=force, only=only))

    def verify(self, *, to: Path | None = None) -> Result:
        return self._cli(self._argv("verify", None, to=to, apply=False))

    def debris(self) -> list[str]:
        # A directory the drill made unreadable cannot hold debris we can see: skip it instead of raising
        # (Linux raises PermissionError from rglob/iterdir where macOS lists nothing).
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.state, onerror=lambda err: None):
            for name in (*dirnames, *filenames):
                if ".restore-" in name and ".pre-restore-" not in name:
                    found.append((Path(dirpath) / name).relative_to(self.state).as_posix())
        tmp = self.root / "tmp"
        try:
            if tmp.is_dir():
                found += [f"snapshots/tmp/{p.name}" for p in tmp.iterdir()]
        except PermissionError:
            pass
        return found

    def script_restore_latest(self, *, snapshots: Path | None = None, state: Path | None = None,
                              force: bool = False, apply: bool = True, ref: str = "latest",
                              shadow: Path | None = None, driver: tuple[str, ...] | None = None) -> tuple[Result, dict]:
        args = ["state", "restore", ref, "--state", str(state or self.state),
                "--snapshots", str(snapshots or self.root)]
        if apply:
            args.append("--apply")
        if force:
            args.append("--force")
        drv = None
        if driver:
            drv = self.sandbox / "driver.py"
            drv.write_text(DRIVER)
        shadow = shadow or self._shadow()
        res = run_script(*args, cwd=shadow, shadow=shadow, driver=drv, driver_args=driver or ())
        return res, _tree_digest(state or self.state)

    def _shadow(self) -> Path:
        d = self.sandbox / "shadow"
        if not d.exists():
            (d / "axiom").mkdir(parents=True)
            (d / "axiom" / "__init__.py").write_text("raise ImportError('axiom is broken (shadow)')\n")
        return d


# ===========================================================================
# vault backups
# ===========================================================================

FILE_ENTRY = "file:identity/private.pem"


class VaultHarness(Harness):
    kind = "vault"
    hows = frozenset({"bitflip", "truncate", "zero", "delete", "manifest-edit", "chain-break", "wrong-version",
                      "foreign-key"})

    def __init__(self, sandbox: Path, state_dir: Path) -> None:
        super().__init__(sandbox, state_dir)
        from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
        from axiom.infra import vault_backup as vb
        from axiom.infra.vault_backup.policy import default_root

        self.vb = vb
        self.root = default_root(state_dir)
        self.store = ForeignCredentialStore(state_dir)
        ki = vb.init_key(state_dir)
        self.code = ki.recovery_code
        self.code_file = sandbox / "code.txt"
        self.code_file.write_text(self.code + "\n")
        self.code_file.chmod(0o600)
        self.secrets = [self.code, PASSPHRASE, CANARY_A, CANARY_B, "CANARY-bc-keyfile"]
        self.wrong_code_file = sandbox / "wrong-code.txt"
        self.wrong_code_file.write_text(ki.recovery_code[::-1] + "\n")
        self.wrong_code_file.chmod(0o600)
        self.secrets.append(ki.recovery_code[::-1])

    def _set_gen(self, g: int) -> None:
        self.store.set("alpha-token", f"{CANARY_A}-g{g}".encode(), provider="guided")
        self.store.set("bravo-key", f"{CANARY_B}-g{g}".encode(), provider="guided")
        (self.state / "identity").mkdir(exist_ok=True)
        (self.state / "identity" / "private.pem").write_text(f"CANARY-bc-keyfile-g{g}")

    def make_generations(self, n: int = GEN_COUNT, wrong_key_newest: bool = False, second_copy: bool = False,
                         **kw: Any) -> None:
        if second_copy:
            self.second = self.sandbox / "second-disk" / "vault-backups"
            self.state.joinpath("settings.toml").write_text(f'[vault.backup]\nsecond_copy = "{self.second}"\n')
        for g in range(1, n + 1):
            self._set_gen(g)
            pw = PASSPHRASE if (wrong_key_newest and g == n) else None
            res = self.vb.create_backup(self.state, force=True, passphrase=pw)
            assert res.status == "created", res
            self.ids.append(res.id)
            self.gen_digests[g] = self.digest()
        self.store.set("local-only", b"local-only-value-xyz", provider="guided")

    def break_target(self) -> None:
        self.store.remove("alpha-token")
        self.store.remove("bravo-key")
        (self.state / "identity" / "private.pem").unlink()

    def digest(self) -> dict[str, str]:
        from axiom.infra.vault_backup import sources as S

        return S.current_fingerprints(self.state)

    def _archive(self, bid: str) -> Path:
        return self.root / "archives" / f"{bid}.axvb"

    def damage(self, bid: str, how: str) -> None:
        a = self._archive(bid)
        ledger = self.root / "ledger.jsonl"
        if how == "bitflip":
            D.bit_flip(a)
        elif how == "truncate":
            D.truncate(a)
        elif how == "zero":
            D.zero_length(a)
        elif how == "delete":
            a.unlink()
        elif how == "manifest-edit":  # the cleartext header is covered by the ledger hash AND the AEAD
            data = a.read_bytes()
            i = data.index(b'"host"')
            a.write_bytes(data[:i] + b'"hosT"' + data[i + 6:])
        elif how == "chain-break":  # the ledger line for this backup is edited, nothing re-hashed
            def edit(entries):
                for e in entries:
                    if e["id"] == bid:
                        e["prev"] = "0" * 64
                return entries

            D.rewrite_ledger(ledger, edit, rehash=False)
        elif how == "wrong-version":  # a backup written by a newer format, recorded consistently
            data = a.read_bytes()
            assert b'"format":1' in data
            a.write_bytes(data.replace(b'"format":1', b'"format":2', 1))

            def fix(entries):
                for e in entries:
                    if e["id"] == bid:
                        e["detail"]["sha256"] = bc.sha256_bytes(a.read_bytes())
                return entries

            D.rewrite_ledger(ledger, fix, rehash=True)
            # a backup a NEWER Axiom wrote elsewhere: this host never recorded its head
            (self.state / "secrets" / "vault-backup-head.json").unlink()
        elif how == "foreign-key":  # made with another key from the start (see make_generations)
            raise ValueError("foreign-key is set up by make_generations(wrong_key_newest=True)")
        else:  # pragma: no cover
            raise ValueError(how)

    def _cli(self, argv: list[str]) -> Result:
        from axiom.extensions.builtins.vault import backup_cli

        return call(lambda: backup_cli.main(argv), capture=self.capture)

    def restore_latest(self, *, to: Path | None = None, force: bool = False, overwrite: bool = False,
                       skip_pre_backup: bool = False, code_file: Path | None = None, apply: bool = True,
                       only: tuple[str, ...] = ()) -> Result:
        return self.restore_id(None, to=to, force=force, overwrite=overwrite, skip_pre_backup=skip_pre_backup,
                               code_file=code_file, apply=apply, only=only)

    def restore_id(self, bid: str | None, *, to: Path | None = None, force: bool = False, overwrite: bool = False,
                   skip_pre_backup: bool = False, code_file: Path | None = None, apply: bool = True,
                   only: tuple[str, ...] = ()) -> Result:
        argv = ["restore"] + ([bid] if bid else []) + ["--code-file", str(code_file or self.code_file)]
        if apply:
            argv.append("--apply")
        if to:
            argv += ["--from", str(to)]
        for flag, on in (("--force", force), ("--overwrite", overwrite), ("--skip-pre-backup", skip_pre_backup)):
            if on:
                argv.append(flag)
        if only:
            argv += ["--only", *only]
        return self._cli(argv)

    def verify(self, *, deep: bool = False, to: Path | None = None) -> Result:
        argv = ["verify"] + (["--deep", "--code-file", str(self.code_file)] if deep else [])
        if to:
            argv += ["--from", str(to)]
        return self._cli(argv)

    def debris(self) -> list[str]:
        return [f"archives/{n}" for n in self.vb.engine.BackupStore(self.root).stray_temp_files()] + [
            p.name for p in self.state.rglob("*") if p.name.startswith(".tmp-")]

    # The script opens a backup DIRECTORY with the code and writes plain files to a NEW dir.
    NAME_OF = {"alpha-token": "alpha-token", "bravo-key": "bravo-key", "file__identity_private.pem": FILE_ENTRY}

    def script_restore_latest(self, *, source: Path | None = None, code_file: Path | None = None,
                              shadow: Path | None = None, **kw: Any) -> tuple[Result, dict[str, str]]:
        from axiom.infra.vault_backup import sources as S

        dest = self.sandbox / "extracted"
        shutil.rmtree(dest, ignore_errors=True)
        shadow = shadow or self._shadow()
        res = run_script("vault", "extract", str(source or self.root), "--to", str(dest),
                         "--code-file", str(code_file or self.code_file), cwd=shadow, shadow=shadow)
        digest: dict[str, str] = {}
        if dest.is_dir():
            for f in dest.iterdir():
                name = self.NAME_OF.get(f.name)
                if name:
                    digest[name] = S.fingerprint_of(f.read_bytes())
        return res, digest

    def _shadow(self) -> Path:
        d = self.sandbox / "shadow"
        if not d.exists():
            (d / "axiom").mkdir(parents=True)
            (d / "axiom" / "__init__.py").write_text("raise ImportError('axiom is broken (shadow)')\n")
        return d


# ===========================================================================
# pg_dump files (the external binaries are stubs; no database is involved)
# ===========================================================================


class PgHarness(Harness):
    kind = "pg"
    hows = frozenset({"bitflip", "truncate", "zero", "manifest-edit"})  # (a deleted dump is simply absent)
    URL = "postgresql://stub/none"

    def __init__(self, sandbox: Path, state_dir: Path, monkeypatch: Any) -> None:
        super().__init__(sandbox, state_dir)
        self.root = sandbox / "pgbackups"
        self.root.mkdir()
        self.bin = sandbox / "bin"
        self.bin.mkdir()
        self.log = sandbox / "calls.log"
        self.mp = monkeypatch
        for name, body in {
            "pg_dump": ("out = sys.argv[sys.argv.index('-f') + 1]\n"
                        "open(out, 'w').write('-- PostgreSQL database dump\\nCREATE TABLE t_' + "
                        "os.environ.get('STUB_GEN', '0') + ' (a int);\\n-- PostgreSQL database dump complete\\n')"),
            "psql": f"open({str(self.log)!r}, 'a').write(sys.argv[-1] + '\\n')",
            "pg_restore": f"open({str(self.log)!r}, 'a').write(sys.argv[-1] + '\\n')",
        }.items():
            p = self.bin / name
            p.write_text(f"#!{sys.executable}\nimport os, sys\n{body}\n")
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("PATH", f"{self.bin}{os.pathsep}{os.environ['PATH']}")

    def make_generations(self, n: int = GEN_COUNT, second_copy: bool = False, **kw: Any) -> None:
        import time

        for g in range(1, n + 1):
            self.mp.setenv("STUB_GEN", str(g))
            r = pg.create_backup(self.URL, output_dir=self.root, label=f"g{g}")
            assert r.success, r.error
            t = time.time() - (n - g) * 3600
            os.utime(r.backup_path, (t, t))
            self.ids.append(r.backup_path.name)
            self.gen_digests[g] = {"restored": r.backup_path.name}
        if second_copy:
            self.second = self.sandbox / "second-disk" / "pgbackups"
            self.second.parent.mkdir()
            D.copy_tree(self.root, self.second)

    def break_target(self) -> None:
        self.log.write_text("")

    def digest(self) -> dict[str, str]:
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        return {"restored": Path(lines[-1]).name if lines else ""}

    def damage(self, bid: str, how: str) -> None:
        f = self.root / bid
        st = f.stat() if f.exists() else None
        self._damage(f, how)
        if st is not None and f.exists():
            os.utime(f, (st.st_atime, st.st_mtime))  # damage must not reorder the backups by touching mtime

    def _damage(self, f: Path, how: str) -> None:
        if how == "bitflip":
            D.bit_flip(f, 0.4)
        elif how == "truncate":
            D.truncate(f, 0.5)
        elif how == "zero":
            D.zero_length(f)
        elif how == "delete":
            f.unlink()
        elif how == "manifest-edit":
            D.edit_json(f.with_name("." + f.name + ".manifest.json"), lambda d: d.__setitem__("sha256", "0" * 64))
        else:  # pragma: no cover
            raise ValueError(how)

    def restore_latest(self, *, to: Path | None = None, **kw: Any) -> Result:
        def run() -> int:
            out = pg.restore_latest(to or self.root, self.URL, confirm=True)
            for n in out.notes:
                print(n)
            if out.error:
                print(f"ERROR: {out.error}")
                print("Try: another directory or a second copy of the dumps")
            return 0 if out.ok else 1

        return call(run, capture=self.capture)

    def restore_id(self, bid: str, **kw: Any) -> Result:
        def run() -> int:
            r = pg.restore_backup(self.root / bid, self.URL, confirm=True)
            if not r.success:
                print(f"ERROR: {r.error}")
                print("Try: restore_latest picks the newest dump that verifies")
            return 0 if r.success else 1

        return call(run)

    def verify(self, **kw: Any) -> Result:
        def run() -> int:
            bad = {b.backup_path.name: pg.verify_backup(b.backup_path) for b in pg.list_backups(self.root)}
            for n, p in bad.items():
                print(f"{n}: {'OK' if not p else 'FAILED ' + '; '.join(p)}")
            return 1 if any(bad.values()) else 0

        return call(run)

    def script_restore_latest(self, **kw: Any) -> tuple[Result, dict[str, str]]:  # pragma: no cover
        raise NotImplementedError("pg dumps are restored with psql/pg_restore; there is no emergency script")

    def debris(self) -> list[str]:
        try:
            return [p.name for p in self.root.iterdir() if p.name.startswith(".tmp-")]
        except OSError:
            return []  # the directory is gone or unreadable: nothing of ours can be in it
