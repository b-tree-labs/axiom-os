#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Emergency restore of harness state and vault backups.  ONE script, standard library only
(plus ``cryptography``, and only for the ``vault`` subcommands).

This file imports nothing from Axiom on purpose: it is for the day the main CLI will not start
(broken install, bad upgrade, missing dependency, a new machine with nothing installed yet).
Run it with the isolated interpreter so a damaged site-packages or a stray module in the
current directory cannot interfere::

    python3 -I axi-restore.py state list
    python3 -I axi-restore.py state verify latest
    python3 -I axi-restore.py state restore latest              # dry run (default)
    python3 -I axi-restore.py state restore latest --apply      # do it

    python3 -I axi-restore.py vault list    PATH...             # names + fingerprints; no key
    python3 -I axi-restore.py vault verify  PATH                # decrypt, compare fingerprints
    python3 -I axi-restore.py vault extract PATH --to DIR       # one 0600 file per credential
    python3 -I axi-restore.py vault reveal  ARCHIVE NAME        # ONE value to stdout, on request

``PATH`` is one ``.axvb`` archive, or a backup directory: then the newest archive that opens
with your code is used and every newer one that did not is listed with why.

STATE (snapshots of harness files; no credential values)
  --state DIR       the state dir to restore INTO (default: $AXI_STATE_DIR, $AXIOM_STATE_DIR,
                    else ~/.axi)
  --snapshots DIR   the snapshot store (default: <state>/snapshots)
  --only GLOB       restore only matching files (repeatable; ``**`` crosses dirs)
  --apply           actually write (without it, restore only prints the diff)
  --force           restore despite a failed verify, a suspect store, running services, or a
                    symlink in the way

  ``latest`` is the newest snapshot that VERIFIES: damaged newer ones are skipped and named.
  If none verifies, nothing is changed and every reason is listed.  Nothing is overwritten in
  place: each file is staged, the current file is kept as ``<name>.pre-restore-<ts>``, the staged
  file replaces it with an atomic rename, and the result is re-hashed.  Nothing is deleted.
  Undo: move the ``.pre-restore-<ts>`` files back.  Re-running after an interruption finishes
  the job (files already restored are skipped).

VAULT (encrypted credential backups)
  WHAT IT NEEDS (exactly): Python 3.11+, the ``cryptography`` package, one backup file (every
  archive carries everything needed except the secret), and the RECOVERY CODE printed at key
  setup (or the passphrase you chose), from a hidden prompt or ``--code-file FILE`` -- never
  the command line.  It does NOT write into the Axiom vault: ``extract`` leaves plain files
  (0600, in a 0700 directory you name, which must not already hold files); put the values back
  with ``<your-cli> secrets set NAME`` once the CLI works, then delete the directory.

  FORMAT (so you can check this script against the spec): MAGIC b"AXVB\\x01\\n" | uint32 BE
  header length | header JSON | AES-256-GCM ciphertext.  AAD = MAGIC + length + header JSON.
  Recipient mode: X25519 + HKDF-SHA256 (salt = eph_pub || recipient_pub, info
  b"axiom-vault-backup/v1/content"); the recipient private key is wrapped with scrypt +
  AES-256-GCM (AAD b"axiom-vault-backup/v1/wrap" + recipient_pub).  Passphrase mode: scrypt.
"""

from __future__ import annotations

import argparse
import base64
import errno
import fnmatch
import getpass
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import sys
import tempfile
import unicodedata
import zlib
from datetime import UTC, datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

ME = "axi-restore.py"


def say(msg: str = "") -> None:
    print(msg)


def die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


# ===========================================================================
# state: harness-state snapshots
# ===========================================================================


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def canon(o) -> bytes:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def glob_match(pattern: str, rel: str) -> bool:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.match("^" + "".join(out) + "$", rel) is not None


def unavailable_reason(root: Path) -> str | None:
    try:
        if not root.exists() and not root.is_symlink():
            return f"{root} does not exist (renamed, deleted, or on a disk that is not mounted)"
        if not root.is_dir():
            return f"{root} is not a directory (something replaced it with a file)"
        if not os.access(root, os.R_OK | os.X_OK):
            return f"{root} is not readable by this user (permission denied)"
    except OSError as exc:
        return f"{root} cannot be examined ({exc.strerror or exc.errno})"
    return None


class Store:
    def __init__(self, root: Path, state: Path):
        self.root = root
        self.state = state
        self.snaps = root / "snaps"
        self.blobs = root / "blobs"
        self._ledger = None

    def ids(self):
        try:
            if not self.snaps.is_dir():
                return []
            return sorted(p.name for p in self.snaps.iterdir() if (p / "manifest.json").is_file())
        except OSError:
            return []

    def manifest(self, sid):
        return json.loads((self.snaps / sid / "manifest.json").read_text("utf-8"))

    def blob(self, sha):
        return self.blobs / sha[:2] / sha

    def read_blob(self, sha) -> bytes:
        with gzip.open(self.blob(sha), "rb") as f:
            return f.read()

    def created(self, sid):
        try:
            return self.manifest(sid)["body"]["created_at"]
        except Exception:  # noqa: BLE001
            return ""

    # -- ledger -----------------------------------------------------------------
    def ledger(self):
        if self._ledger is None:
            out = []
            try:
                text = (self.root / "ledger.jsonl").read_text("utf-8")
            except OSError:
                text = ""
            for line in text.splitlines():
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        out.append({"_corrupt": True})
            self._ledger = out
        return self._ledger

    def ledger_check(self):
        """``(problems, first_bad)``: entries before ``first_bad`` (1-based) are still vouched for."""
        problems, prev = [], ""
        for i, e in enumerate(self.ledger(), 1):
            if "_corrupt" in e:
                return [f"ledger line {i} is not valid JSON"], i
            body = {k: v for k, v in e.items() if k != "hash"}
            if e.get("prev") != prev:
                problems.append(f"ledger entry {i}: chain does not link to the previous entry")
            if e.get("hash") != sha256_bytes(canon(body)):
                problems.append(f"ledger entry {i}: hash does not match its content")
            if problems:
                return problems, i
            prev = e["hash"]
        return [], None

    def ledger_problems(self):
        return self.ledger_check()[0]

    def head_problems(self):
        """Rollback evidence from the record kept outside the store (the state dir's secrets/)."""
        try:
            rec = json.loads((self.state / "secrets" / "snapshot-head.json").read_text("utf-8"))["roots"][
                str(self.root.resolve())]
            seq = int(rec["seq"])
        except (OSError, KeyError, ValueError, TypeError):
            return []
        entries = self.ledger()
        if not entries and not (self.root / "ledger.jsonl").exists():
            return [f"LEDGER MISSING: this host last wrote ledger entry {seq}, but the store has no ledger"]
        if len(entries) < seq:
            return [f"ROLLBACK SUSPECTED: this host last wrote ledger entry {seq}, but the store only has "
                    f"{len(entries)}; it may have been replaced by an older copy"]
        if entries[seq - 1].get("hash") != rec.get("head"):
            return [f"TAMPER SUSPECTED: ledger entry {seq} differs from what this host recorded"]
        return []

    def order(self):
        """Snapshot ids, most recently TAKEN first (ledger order beats a possibly wrong clock)."""
        seq = {e["id"]: e["seq"] for e in self.ledger()
               if e.get("event") == "snapshot" and e.get("id") and isinstance(e.get("seq"), int)}
        return sorted(self.ids(), key=lambda i: (seq.get(i, 0), self.created(i), i), reverse=True)

    def verify(self, sid):
        problems = []
        try:
            m = self.manifest(sid)
            body = m["body"]
        except Exception as exc:  # noqa: BLE001
            return [f"manifest unreadable: {type(exc).__name__}"]
        body_sha = sha256_bytes(canon(body))
        if body_sha != m.get("body_sha256"):
            problems.append("manifest body does not match its recorded hash")
        if sha256_bytes(f"{m.get('prev', '')}\n{body_sha}".encode()) != m.get("chain_hash"):
            problems.append("chain hash does not match prev + body")
        ver = body.get("version", 1)
        if not problems and (not isinstance(ver, int) or ver > 1):
            problems.append(f"snapshot schema {ver} is newer than this script understands (1); use the "
                            "script from the newer release")
        for f in body.get("files", []):
            p = self.blob(f["sha256"])
            if not p.is_file():
                problems.append(f"{f['path']}: blob missing")
                continue
            try:
                if sha256_bytes(self.read_blob(f["sha256"])) != f["sha256"]:
                    problems.append(f"{f['path']}: blob content does not match its hash")
            except (OSError, EOFError, gzip.BadGzipFile, zlib.error):
                problems.append(f"{f['path']}: blob unreadable")
        events = [e for e in self.ledger() if e.get("event") == "snapshot" and e.get("id")]
        recorded = {e["id"]: e.get("detail", {}).get("chain_hash") for e in events}
        lp, first_bad = self.ledger_check()
        seq = {e["id"]: e.get("seq") for e in events}
        if first_bad is not None and isinstance(seq.get(sid), int) and seq[sid] >= first_bad:
            problems.append(f"its ledger entry ({seq[sid]}) is past a break in the ledger chain "
                            f"(entry {first_bad}: {lp[0]}), so the ledger cannot vouch for it")
        if recorded:
            if sid not in recorded:
                problems.append("snapshot is not in the ledger (added outside the tool?)")
            elif recorded[sid] != m.get("chain_hash"):
                problems.append("manifest differs from the one the ledger recorded (substituted)")
        return problems

    def choose(self, ref, force):
        """``(sid, notes)`` or raise SystemExit with every reason."""
        ids = self.ids()
        if not ids:
            why = unavailable_reason(self.root)
            die((why + ". " if why else f"no snapshots in {self.root}. ")
                + f"If they are elsewhere: python3 -I {ME} state list --snapshots DIR")
        if ref in ("latest", "latest-verified"):
            hp = self.head_problems()
            if hp and not force:
                for p in hp:
                    say(p)
                say(f"REFUSING to guess which snapshot is latest from a store that may have been rolled back or "
                    f"emptied.\nnext: python3 -I {ME} state list   then name one;  or --force")
                raise SystemExit(1)
            notes, rejected = [], []
            for sid in self.order():
                problems = self.verify(sid)
                if not problems:
                    for sk, why in rejected:
                        notes.append(f"skipped snapshot {sk}: {why[0]}" + (f" (+{len(why) - 1} more)" if len(why) > 1 else ""))
                    if rejected:
                        notes.append(f"using the newest snapshot that verifies: {sid}")
                    return sid, notes
                rejected.append((sid, problems))
            say(f"no snapshot in {self.root} passes verification, so nothing was changed. Why each failed:")
            for sk, why in rejected:
                say(f"  {sk}: " + "; ".join(why[:3]))
            say(f"next: try another location or a second copy: python3 -I {ME} state verify latest --snapshots DIR")
            if force and rejected:
                say("--force: continuing with the newest, restoring only what is intact")
                return rejected[0][0], notes
            raise SystemExit(1)
        if ref in ids:
            return ref, [f"WARNING: {p}" for p in self.head_problems() + self.ledger_problems()]
        hits = [i for i in ids if i.startswith(ref)]
        if len(hits) == 1:
            return hits[0], [f"WARNING: {p}" for p in self.head_problems() + self.ledger_problems()]
        die(f"{'ambiguous' if hits else 'unknown'} snapshot {ref!r}; try: python3 -I {ME} state list")


def confine(state: Path, rel: str) -> Path:
    parts = rel.split("/")
    if not rel or rel.startswith("/") or "\\" in rel or "\0" in rel or any(p in ("", ".", "..") for p in parts):
        die(f"refusing unsafe path in manifest: {rel!r}")
    base = os.path.realpath(state)
    # the parent chain may not leave the state dir; the last component is not followed (a symlink
    # there is a conflict to report, and a restore replaces it rather than writing through it)
    parent = os.path.realpath(os.path.join(base, *parts[:-1]))
    if parent != base and not parent.startswith(base + os.sep):
        die(f"refusing path that resolves outside the state dir: {rel!r}")
    return Path(parent, parts[-1])


def running_services(state: Path, max_depth=3):
    found = []

    def scan(d: Path, depth: int, rel: str):
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            r = f"{rel}/{e.name}" if rel else e.name
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if depth < max_depth and r not in ("snapshots", "service-venv"):
                        scan(Path(e.path), depth + 1, r)
                    continue
            except OSError:
                continue
            if e.name.endswith(".pid"):
                try:
                    pid = int(Path(e.path).read_text().split()[0])
                    if pid != os.getpid():
                        os.kill(pid, 0)
                        found.append(f"{r} (pid {pid} alive)")
                except PermissionError:
                    found.append(f"{r} (pid alive)")
                except (OSError, ValueError, IndexError):
                    pass
            elif e.name.endswith(".lock") and fcntl is not None:
                try:
                    fd = os.open(e.path, os.O_RDONLY)
                except OSError:
                    continue
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError as exc:
                    if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                        found.append(f"{r} (lock held)")
                finally:
                    os.close(fd)

    scan(state, 0, "")
    return found


def plan(store: Store, sid: str, state: Path, only):
    out = []
    for f in store.manifest(sid)["body"]["files"]:
        rel = f["path"]
        if only and not any(glob_match(g, rel) for g in only):
            continue
        t = confine(state, rel)
        if t.is_symlink():
            out.append((rel, "conflict", f, "a symlink is in the way"))
        elif t.exists() and not t.is_file():
            out.append((rel, "conflict", f, "something that is not a regular file is in the way"))
        elif not t.is_file():
            out.append((rel, "create", f, ""))
        else:
            try:
                same = sha256_file(t) == f["sha256"]
            except OSError as exc:
                out.append((rel, "conflict", f, f"the current file cannot be read ({exc.strerror})"))
                continue
            if not same:
                out.append((rel, "replace", f, ""))
            elif stat.S_IMODE(t.stat().st_mode) != f.get("mode"):
                out.append((rel, "chmod", f, ""))
            else:
                out.append((rel, "unchanged", f, ""))
    return out


def install(store: Store, f: dict, target: Path, stamp: str, replace_link: bool = False):
    data = store.read_blob(f["sha256"])
    if sha256_bytes(data) != f["sha256"]:
        raise RuntimeError("blob does not match its hash")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.restore-")
    aside = None
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, f.get("mode", 0o600) & 0o7777)
        if target.exists() or target.is_symlink():
            aside = target.with_name(f"{target.name}.pre-restore-{stamp}")
            n = 1
            while aside.exists() or aside.is_symlink():
                n += 1
                aside = target.with_name(f"{target.name}.pre-restore-{stamp}-{n}")
            if target.is_symlink() and replace_link:
                os.rename(target, aside)
            else:
                try:
                    os.link(target, aside)
                except OSError:
                    shutil.copy2(target, aside)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if aside is not None:
            try:
                if aside.is_symlink() and not target.exists() and not target.is_symlink():
                    os.rename(aside, target)
                else:
                    aside.unlink()
            except OSError:
                pass
        raise
    if not (target.is_file() and not target.is_symlink() and sha256_file(target) == f["sha256"]):
        if aside is not None:
            try:
                os.replace(aside, target)
            except OSError:
                pass
        raise RuntimeError("the write did not land (post-restore hash check failed); the original was kept")
    return aside


def state_main(a) -> int:
    state = Path(a.state or os.environ.get("AXI_STATE_DIR") or os.environ.get("AXIOM_STATE_DIR")
                 or Path.home() / ".axi")
    store = Store(Path(a.snapshots) if a.snapshots else state / "snapshots", state)

    if a.command == "list":
        ids = store.order()
        if not ids:
            why = unavailable_reason(store.root)
            die((why + ". " if why else f"no snapshots in {store.root}. ")
                + "Point at another location with --snapshots DIR.")
        for sid in ids:
            try:
                b = store.manifest(sid)["body"]
                say(f"{sid}  {b['created_at']}  files={len(b['files'])}  {b.get('reason', '')}")
            except Exception:  # noqa: BLE001
                say(f"{sid}  (manifest unreadable)")
        say(f"\nnext: python3 -I {sys.argv[0]} state verify latest")
        return 0

    sid, notes = store.choose(a.ref, a.force)
    problems = store.verify(sid)
    if a.command == "verify":
        for n in notes:
            say(n)
        lp = store.ledger_problems()
        if problems:
            say(f"{sid}: FAILED")
            for p in problems:
                say(f"  - {p}")
            say(f"\nnext: python3 -I {sys.argv[0]} state verify latest   (picks the newest intact one)")
            return 1
        say(f"{sid}: OK ({len(store.manifest(sid)['body']['files'])} files verified)")
        for p in lp:
            say(f"  note: {p}")
        say(f"\nnext: python3 -I {sys.argv[0]} state restore {sid}")
        return 0

    steps = plan(store, sid, state, a.only)
    if a.only and not steps:
        say(f"--only {a.only} matched nothing in {sid}")
        return 1
    todo = [s for s in steps if s[1] != "unchanged"]
    say(f"snapshot {sid} -> {state}")
    for n in notes:
        say(n)
    for rel, act, _f, note in todo:
        say(f"  {act:8} {rel}" + (f"   ({note})" if note else ""))
    say(f"  ({len(todo)} change(s), {len(steps) - len(todo)} already identical; nothing is deleted)")
    if not a.apply:
        say(f"\nDRY RUN. next: python3 -I {sys.argv[0]} state restore {sid} --apply")
        return 0
    if problems and not a.force:
        say(f"REFUSING: snapshot {sid} fails verification ({problems[0]}).")
        say(f"next: python3 -I {sys.argv[0]} state restore latest --apply   (or --force)")
        return 1
    live = running_services(state)
    if live and not a.force:
        say("REFUSING: services appear to be running: " + "; ".join(live[:5]))
        say("Stop them (or pass --force), then re-run.")
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    failed, aside, done = {}, {}, 0
    for rel, act, f, note in todo:
        target = confine(state, rel)
        if act == "conflict" and not (a.force and target.is_symlink()):
            failed[rel] = note
            continue
        try:
            if act == "chmod":
                os.chmod(target, f.get("mode", 0o600) & 0o7777)
            else:
                p = install(store, f, target, stamp, replace_link=act == "conflict")
                if p:
                    aside[rel] = p
            done += 1
        except Exception as exc:  # noqa: BLE001
            failed[rel] = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
    say(f"\nrestored {done} file(s); {len(aside)} previous version(s) kept as *.pre-restore-{stamp}")
    if failed:
        say(f"PARTIAL RESTORE: {len(failed)} file(s) NOT restored:")
        for rel, why in failed.items():
            say(f"  {rel}: {why}")
        say("Every file is either its old or its new version, never torn. Fix the cause and re-run the same "
            "command: it only touches what still differs.")
    say("next steps:")
    say("  1. restart the Axiom services you stopped")
    say(f"  2. confirm things work, then remove leftovers: find {state} -name '*.pre-restore-{stamp}*' -delete")
    say(f"  3. to undo: move each *.pre-restore-{stamp} file back over its original name")
    say("  4. databases are NOT in snapshots: restore them from a pg_dump (see the runbook) if needed")
    return 1 if failed else 0


# ===========================================================================
# vault: encrypted credential backups
# ===========================================================================

MAGIC = b"AXVB\x01\n"
SUPPORTED_FORMAT = 1
GENERIC = "cannot decrypt: wrong key or damaged backup"
_crypto = None


def crypto():
    """Import ``cryptography`` only when a vault subcommand needs it."""
    global _crypto
    if _crypto is None:
        try:
            from cryptography.exceptions import InvalidTag
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric.x25519 import (
                X25519PrivateKey,
                X25519PublicKey,
            )
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            from cryptography.hazmat.primitives.kdf.hkdf import HKDF
            from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        except ImportError:  # pragma: no cover
            die("the vault subcommands need the `cryptography` package: pip install cryptography")
        _crypto = (InvalidTag, hashes, X25519PrivateKey, X25519PublicKey, AESGCM, HKDF, Scrypt)
    return _crypto


class Bad(Exception):
    """A backup that cannot be used; the message is safe to print (no values, no key material)."""


def b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"), validate=True)


def read_header(data: bytes):
    if not data.startswith(MAGIC) or len(data) < len(MAGIC) + 4:
        raise Bad("not a vault backup")
    (hlen,) = struct.unpack(">I", data[len(MAGIC): len(MAGIC) + 4])
    start = len(MAGIC) + 4
    if hlen > 16 * 1024 * 1024 or start + hlen > len(data):
        raise Bad("backup is truncated or damaged")
    try:
        header = json.loads(data[start: start + hlen])
        fmt = int(header["format"])
        mode = header["enc"]["mode"]
    except (ValueError, KeyError, TypeError):
        raise Bad("backup header is damaged") from None
    if fmt > SUPPORTED_FORMAT:
        raise Bad(f"this backup is format {fmt}; this script reads up to {SUPPORTED_FORMAT}. "
                  "Use the script from the newer release.")
    if mode not in ("recipient", "passphrase"):
        raise Bad("backup header is damaged")
    return header, start + hlen


def kdf_params(k):
    n, r, p = int(k["n"]), int(k["r"]), int(k["p"])
    if not (2**10 <= n <= 2**22 and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 8 and 128 * n * r <= 256 * 2**20):
        raise Bad("backup header has unreasonable key-derivation parameters")
    return n, r, p


def check_chars(chars: str) -> str:
    d = hashlib.sha256(b"axiom-vault-backup/recovery-check/" + chars.encode()).digest()
    return base64.b32encode(d[:3]).decode().rstrip("=")[:4]


def material(secret: str, label: str) -> bytes:
    if label == "recovery-code":
        flat = "".join(ch for ch in secret.upper() if ch.isalnum())
        if len(flat) != 36 or check_chars(flat[:32]) != flat[32:]:
            return b"\x00invalid-recovery-code\x00" + secret.encode("utf-8", "replace")
        return flat.encode()
    return unicodedata.normalize("NFKC", secret).encode("utf-8")


def decrypt(data: bytes, secret: str):
    InvalidTag, hashes, X25519PrivateKey, X25519PublicKey, AESGCM, HKDF, Scrypt = crypto()

    def scrypt(mat: bytes, salt: bytes, n: int, r: int, p: int) -> bytes:
        return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(mat)

    header, off = read_header(data)
    enc = header["enc"]
    try:
        nonce = b64d(enc["nonce"])
        if enc["mode"] == "recipient":
            w = enc["wrapped_priv"]
            n, r, p = kdf_params(w)
            pub = b64d(enc["recipient_pub"])
            kek = scrypt(material(secret, w.get("label", "")), b64d(w["salt"]), n, r, p)
            priv = X25519PrivateKey.from_private_bytes(
                AESGCM(kek).decrypt(b64d(w["nonce"]), b64d(w["ct"]), b"axiom-vault-backup/v1/wrap" + pub))
            eph = b64d(enc["eph_pub"])
            key = HKDF(algorithm=hashes.SHA256(), length=32, salt=eph + pub,
                       info=b"axiom-vault-backup/v1/content").derive(priv.exchange(X25519PublicKey.from_public_bytes(eph)))
        else:
            n, r, p = kdf_params(enc)
            key = scrypt(material(secret, "passphrase"), b64d(enc["salt"]), n, r, p)
        body = json.loads(AESGCM(key).decrypt(nonce, data[off:], data[:off]))
        entries = {k: {**{a: b for a, b in v.items() if a != "value"}, "value": base64.b64decode(v["value"])}
                   for k, v in body["entries"].items()}
    except (InvalidTag, ValueError, KeyError, TypeError):
        raise Bad(GENERIC) from None
    return header, entries


def fingerprint(value: bytes) -> str:
    return hashlib.sha256(value.decode("utf-8", "replace").encode()).hexdigest()[:16]


def get_secret(args) -> str:
    if args.code_file:
        try:
            st = os.stat(args.code_file)
            if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                print(f"warning: {args.code_file} is readable by others; chmod 600 it", file=sys.stderr)
            with open(args.code_file, encoding="utf-8") as f:
                return f.read().strip()
        except OSError as exc:
            die(f"cannot read {args.code_file}: {exc.strerror}. Put the recovery code (or passphrase) in a file only "
                "you can read (chmod 600) and pass --code-file FILE. If the code is lost, no one can open these "
                "backups: re-issue the credentials (`<your-cli> secrets set NAME`) and make a new key.")
    if not sys.stdin.isatty():
        die("no terminal to ask for the recovery code; use --code-file FILE")
    return getpass.getpass("Recovery code or passphrase: ")


def load(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as exc:
        die(f"cannot read {path}: {exc.strerror}")


def archives_in(path: str) -> tuple[list[str], list[tuple[str, str]]]:
    """``(usable, unusable)`` for a backup directory: archives whose (unencrypted) header reads, newest
    first by the time in it, and those whose header does not, with why.  The latter cannot be placed in
    time, so they are always reported: one of them may be the newest backup."""
    root = Path(path)
    arch = root / "archives" if (root / "archives").is_dir() else root
    rows, bad = [], []
    try:
        names = [p for p in arch.iterdir() if p.name.endswith(".axvb") and not p.name.startswith(".")]
    except OSError as exc:
        die(f"cannot read {path}: {exc.strerror or exc.errno}")
    for p in names:
        try:
            created = read_header(p.read_bytes())[0].get("created_at", "")
        except (Bad, OSError) as exc:
            bad.append((str(p), str(exc) if isinstance(exc, Bad) else f"cannot read it ({exc.strerror})"))
            continue
        rows.append((created, p.name, str(p)))
    return [r[2] for r in sorted(rows, reverse=True)], sorted(bad)


def bad_archive(exc: Exception):
    """Refuse an unusable backup with the ways out (the message itself is value-free)."""
    die(f"{exc}. Try a second copy of the backups or an older archive from the same directory (point this "
        "command at the directory to use the newest that opens). A directory that something replaced "
        "with a file reads as 'not a vault backup'.")


def need_exists(path: str) -> None:
    if not os.path.exists(path):
        die(f"{path} does not exist (renamed, deleted, or on a disk that is not mounted). Try a second copy "
            "or another location of the backups.")
    if os.path.isdir(path) and not os.access(path, os.R_OK | os.X_OK):
        die(f"{path} is not readable by this user (permission denied). Fix its permissions or try a second copy.")


def expand(paths) -> list[str]:
    out = []
    for p in paths:
        need_exists(p)
        if os.path.isdir(p):
            found = archives_in(p)[0]
            if not found:
                die(f"no .axvb archives in {p} (is the backup directory missing, renamed, or on an "
                    "unmounted disk? point at another copy)")
            out += found
        else:
            out.append(p)
    return out


def open_newest(path: str, secret: str):
    """``(archive_path, header, entries)`` of the newest archive that opens; notes on stderr."""
    usable, unusable = archives_in(path)
    if not usable and not unusable:
        die(f"no .axvb archives in {path} (is the backup directory missing, renamed, or on an unmounted disk? "
            "try a second copy or another location)")
    skipped = [(ap, f"{why} (its date cannot be read, so it may be newer than the one used)")
               for ap, why in unusable]
    for ap in usable:
        try:
            header, entries = decrypt(load_or_bad(ap), secret)
        except Bad as exc:
            skipped.append((ap, str(exc)))
            continue
        for sk, why in skipped:
            print(f"skipped {sk}: {why}", file=sys.stderr)
        if skipped:
            print(f"using the newest backup that opens: {ap}", file=sys.stderr)
        return ap, header, entries
    for sk, why in skipped:
        print(f"{sk}: {why}", file=sys.stderr)
    die(GENERIC + ". If you have the right code, try another copy of the backups (second copy, other disk); "
        "if the code is lost, a backup cannot be opened by anyone and the credentials must be re-issued.")


def load_or_bad(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as exc:
        raise Bad(f"cannot read it ({exc.strerror})") from None


def show_archive(path: str, header: dict) -> None:
    print(f"{path}: {header.get('id')}  {header.get('created_at')}  host={header.get('host')}  "
          f"{header.get('count')} value(s)  unlock={header['enc']['mode']}")
    for m in header.get("entries", []):
        print(f"    {m['name']:36} {m.get('scope', ''):14} {m.get('fingerprint', '')}")


def cmd_list(args):
    unusable = 0
    for given in args.archive:
        need_exists(given)
        if not os.path.isdir(given):  # a file named on purpose: refuse it if it is not a backup
            try:
                header, _ = read_header(load_or_bad(given))
            except Bad as exc:
                die(str(exc))
            show_archive(given, header)
            continue
        usable, bad = archives_in(given)  # a directory: show everything, flag what cannot be read
        if not usable and not bad:
            die(f"no .axvb archives in {given} (is the backup directory missing, renamed, or on an unmounted "
                "disk? try a second copy or another location)")
        for path, why in bad:
            print(f"{path}: UNUSABLE ({why})")
            unusable += 1
        for path in usable:
            show_archive(path, read_header(load_or_bad(path))[0])
    if unusable:
        raise SystemExit(1)


def check_entries(header, entries) -> list[str]:
    return [m["name"] for m in header["entries"] if m.get("scope") == "value"
            and (m["name"] not in entries or fingerprint(entries[m["name"]]["value"]) != m["fingerprint"])]


def cmd_verify(args):
    need_exists(args.archive)
    secret = get_secret(args)
    if os.path.isdir(args.archive):
        bad_total = 0
        for ap in expand([args.archive]):
            try:
                header, entries = decrypt(load_or_bad(ap), secret)
                bad = check_entries(header, entries)
                print(f"{ap}: " + ("OK" if not bad else "FINGERPRINT MISMATCH for " + ", ".join(bad)))
                bad_total += bool(bad)
            except Bad as exc:
                print(f"{ap}: FAILED ({exc})")
                bad_total += 1
        raise SystemExit(1 if bad_total else 0)
    try:
        header, entries = decrypt(load_or_bad(args.archive), secret)
    except Bad as exc:
        bad_archive(exc)
    bad = check_entries(header, entries)
    if bad:
        die("fingerprint mismatch for: " + ", ".join(bad))
    print(f"ok: {len(entries)} value(s) decrypted and matched their manifest fingerprints")


def write_new(dest: str, name: str, value: bytes) -> None:
    fd = os.open(os.path.join(dest, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, value)
        os.fsync(fd)
    finally:
        os.close(fd)


def cmd_extract(args):
    need_exists(args.archive)
    secret = get_secret(args)
    if os.path.isdir(args.archive):
        _ap, header, entries = open_newest(args.archive, secret)
    else:
        try:
            header, entries = decrypt(load_or_bad(args.archive), secret)
        except Bad as exc:
            bad_archive(exc)
    dest = args.to
    if os.path.exists(dest) and os.listdir(dest):
        die(f"{dest} already holds files; give an empty or new directory")
    os.makedirs(dest, mode=0o700, exist_ok=True)
    os.chmod(dest, 0o700)
    n = 0
    for name in sorted(entries):
        if args.only and not any(fnmatch.fnmatchcase(name, g) for g in args.only):
            continue
        fname = name.replace("file:", "file__").replace("/", "_").replace("~", "HOME")
        try:
            write_new(dest, fname, entries[name]["value"])
        except OSError as exc:
            die(f"could not write {fname}: {exc.strerror or exc.errno}. {n} value(s) were written to {dest} "
                "before this; fix the cause (disk space, permissions) and extract into a NEW directory.")
        n += 1
        print(f"wrote {fname}")
    print(f"{n} value(s) written to {dest} (0600). Put them back with `<your-cli> secrets set NAME`, then delete {dest}.")


def cmd_reveal(args):
    need_exists(args.archive)
    secret = get_secret(args)
    try:
        _, entries = decrypt(load_or_bad(args.archive), secret)
    except Bad as exc:
        bad_archive(exc)
    if args.name not in entries:
        die(f"no entry named {args.name!r} in this backup")
    sys.stdout.buffer.write(entries[args.name]["value"])
    sys.stdout.buffer.flush()


def vault_main(args) -> int:
    {"list": cmd_list, "verify": cmd_verify, "extract": cmd_extract, "reveal": cmd_reveal}[args.vcmd](args)
    return 0


# ===========================================================================


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=ME, description=__doc__.split("\n\n")[0])
    top = p.add_subparsers(dest="area", required=True)

    st = top.add_parser("state", help="restore harness state from snapshots")
    st.add_argument("command", choices=["list", "verify", "restore"])
    st.add_argument("ref", nargs="?", default="latest")
    st.add_argument("--state")
    st.add_argument("--snapshots")
    st.add_argument("--only", action="append", default=[])
    st.add_argument("--apply", action="store_true")
    st.add_argument("--force", action="store_true")

    vt = top.add_parser("vault", help="open encrypted vault backups")
    sub = vt.add_subparsers(dest="vcmd", required=True)
    s = sub.add_parser("list")
    s.add_argument("archive", nargs="+")
    for name in ("verify", "extract", "reveal"):
        s = sub.add_parser(name)
        s.add_argument("archive")
        s.add_argument("--code-file")
        if name == "extract":
            s.add_argument("--to", required=True)
            s.add_argument("--only", nargs="+")
        if name == "reveal":
            s.add_argument("name")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.area == "state":
        return state_main(args)
    return vault_main(args)


if __name__ == "__main__":
    sys.exit(main())
