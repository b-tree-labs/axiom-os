# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Generate the committed compatibility fixtures.

These were produced by the PRE-refactor code (commit 9af8fa4e, before
``axiom.infra.backup_core`` existed) and are committed as bytes.  The
compatibility tests assert today's code still verifies and restores them, so a
refactor that changes an on-disk format fails loudly.

Run only to regenerate on purpose (it overwrites the fixtures):

    AXIOM_STATE_DIR=<tmp> HOME=<tmp> python3 make_fixtures.py

Everything here is fabricated.  The "recovery code" and passphrase protect
nothing real.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PASSPHRASE = "fixture passphrase, protects nothing 31"


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="axiom-fixture-"))
    for k in ("HOME", "AXI_HOME", "AXI_STATE_DIR", "AXIOM_STATE_DIR"):
        os.environ[k] = str(tmp / k.lower())
        Path(os.environ[k]).mkdir(parents=True, exist_ok=True)
    os.environ["AXIOM_FOREIGN_SECRETS_BACKEND"] = "file"
    os.environ["AXIOM_FILE_SECRETS_PATH"] = str(tmp / "file-secrets.json")
    os.environ["AXIOM_ACTION_LEDGER_BACKEND"] = "jsonl"
    os.environ.pop("AXIOM_DB_URL", None)
    assert Path(os.environ["AXIOM_STATE_DIR"]).is_relative_to(tmp)

    from axiom.infra import state_snapshots as ss

    state = Path(os.environ["AXIOM_STATE_DIR"])
    (state / "identity").mkdir()
    (state / "settings.toml").write_text('[a]\nb = 1\n')
    (state / "identity" / "node.json").write_text('{"owner": "fixture"}')
    root = tmp / "snapshots"
    r1 = ss.create_snapshot(state, root, reason="fixture one", force=True)
    (state / "settings.toml").write_text('[a]\nb = 2\n')
    (state / "memory").mkdir()
    (state / "memory" / "ledger.jsonl").write_text('{"n":1}\n')
    r2 = ss.create_snapshot(state, root, reason="fixture two", force=True)
    ss.set_pinned(ss.Store(root), r1.snapshot_id, True)
    ss.restore_snapshot(state, root, r1.snapshot_id, apply=True, force=True, _services=lambda _s: [])
    dst = HERE / "snapshots_v1"
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(root, dst)
    for stray in dst.rglob("*"):
        if stray.name == ".lock":
            stray.unlink()
    expected = {
        "ids": [r1.snapshot_id, r2.snapshot_id],
        "files_at": {r1.snapshot_id: {"settings.toml": '[a]\nb = 1\n', "identity/node.json": '{"owner": "fixture"}'},
                     r2.snapshot_id: {"settings.toml": '[a]\nb = 2\n', "memory/ledger.jsonl": '{"n":1}\n',
                                      "identity/node.json": '{"owner": "fixture"}'}},
    }
    (HERE / "snapshots_v1.expected.json").write_text(json.dumps(expected, indent=1, sort_keys=True))

    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
    from axiom.infra import vault_backup as vb
    from axiom.infra.vault_backup import crypto

    crypto.DEFAULT_KDF = {"n": 2**10, "r": 8, "p": 1}
    store = ForeignCredentialStore(state)
    store.set("fixture-alpha", b"FIXTURE-VALUE-ALPHA-0001", provider="guided")
    store.set("fixture-bravo", b"FIXTURE-VALUE-BRAVO-0002", provider="guided")
    ki = vb.init_key(state)
    vroot = tmp / "vault-backups"
    b1 = vb.create_backup(state, root=vroot, reason="fixture recipient")
    store.set("fixture-alpha", b"FIXTURE-VALUE-ALPHA-0003", provider="guided")
    b2 = vb.create_backup(state, root=vroot, reason="fixture recipient two")
    b3 = vb.create_backup(state, root=vroot, passphrase=PASSPHRASE, force=True, reason="fixture passphrase")
    vdst = HERE / "vault_v1"
    shutil.rmtree(vdst, ignore_errors=True)
    shutil.copytree(vroot, vdst)
    for stray in vdst.rglob(".lock"):
        stray.unlink()
    (HERE / "vault_v1.recovery-code.txt").write_text(ki.recovery_code + "\n")
    shutil.copy(state / "secrets" / "vault-backup-recipient.json", HERE / "vault_v1.recipient.json")
    (HERE / "vault_v1.expected.json").write_text(json.dumps({
        "ids": [b1.id, b2.id, b3.id], "passphrase": PASSPHRASE, "passphrase_id": b3.id,
        "values_at": {b1.id: {"fixture-alpha": "FIXTURE-VALUE-ALPHA-0001", "fixture-bravo": "FIXTURE-VALUE-BRAVO-0002"},
                      b2.id: {"fixture-alpha": "FIXTURE-VALUE-ALPHA-0003", "fixture-bravo": "FIXTURE-VALUE-BRAVO-0002"}},
    }, indent=1, sort_keys=True))
    shutil.rmtree(tmp, ignore_errors=True)
    print("fixtures written", file=sys.stderr)


if __name__ == "__main__":
    main()
