# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The CLI verbs, the skill registration, and the property that matters most:
no value, key or recovery code ever appears anywhere it should not.

A canary is a fabricated secret.  After a full lifecycle (set, backup, restore, scrub,
exposure, failures) we search stdout, stderr, log records, exception messages, the cleartext
manifests, the ledger, the audit trail and the archive bytes for it, raw and base64.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import pytest

from axiom.extensions.builtins.vault import backup_cli
from axiom.infra import vault_backup as vb
from axiom.infra.vault_backup import crypto as C

from .conftest import CANARY_A, CANARY_B, CANARY_FILE


def run(capsys, *argv, expect=None):
    rc = backup_cli.main(list(argv))
    out = capsys.readouterr()
    if expect is not None:
        assert rc == expect, (argv, out.out, out.err)
    return rc, out.out, out.err


def code_file(tmp_path, code):
    f = tmp_path / f"unlock-{abs(hash(code))}.txt"
    f.write_text(code + "\n")
    f.chmod(0o600)
    return str(f)


@pytest.fixture
def key_via_cli(capsys, tmp_path, fast_kdf, state_dir):
    out_file = tmp_path / "recovery-code.txt"
    rc, out, err = run(capsys, "key-init", "--code-out", str(out_file), expect=0)
    code = out_file.read_text().strip()
    assert C.looks_like_recovery_code(code)
    return code, out_file


# -- registration -------------------------------------------------------------


def test_every_cli_verb_is_a_registered_skill():
    from axiom.extensions.builtins.vault import skills

    reg = skills.bind_default()
    for verb, cap in backup_cli.VERBS.items():
        assert reg.has(cap), (verb, cap)
        assert reg.spec(cap).description


def test_the_manifest_declares_every_backup_skill_and_the_schedule():
    import tomllib

    p = Path(__file__).resolve().parents[3] / "src/axiom/extensions/builtins/vault/axiom-extension.toml"
    m = tomllib.loads(p.read_text())
    declared = {x["name"] for x in m["extension"]["provides"] if x["kind"] == "skill"}
    assert {"vault.backup", "vault.backup_restore", "vault.backup_scrub", "vault.key_init",
            "vault.backup_tick"} <= declared
    assert any(s["action"] == "vault.backup_tick" for s in m["extension"]["schedule"])
    assert "key-init" in next(x for x in m["extension"]["provides"] if x["kind"] == "cmd")["subcommands"]


def test_every_skill_entry_in_the_manifest_resolves():
    import importlib
    import tomllib

    p = Path(__file__).resolve().parents[3] / "src/axiom/extensions/builtins/vault/axiom-extension.toml"
    for x in tomllib.loads(p.read_text())["extension"]["provides"]:
        if x["kind"] == "skill":
            mod, fn = x["entry"].split(":")
            assert callable(getattr(importlib.import_module(mod), fn))


def test_next_steps_use_the_brand_helper(capsys, state_dir, vault, monkeypatch, key_via_cli):
    class B:
        cli_name = "zedcli"

    monkeypatch.setattr("axiom.infra.branding.get_branding", lambda: B())
    rc, out, err = run(capsys, "backup", expect=0)
    assert "zedcli vault" in out and "axi vault" not in out + err
    rc, out, _ = run(capsys, "key-info", expect=0)
    assert "zedcli vault" in out and "axi vault" not in out


# -- verbs end to end ---------------------------------------------------------------


def test_lifecycle_through_the_cli(capsys, tmp_path, state_dir, vault, key_via_cli):
    code, _ = key_via_cli
    cf = code_file(tmp_path, code)
    rc, out, _ = run(capsys, "backup", expect=0)
    assert "backed up 2 credential value(s)" in out and "Next:" in out
    rc, out, _ = run(capsys, "ls", expect=0)
    assert "2 entries" in out
    rc, out, _ = run(capsys, "show", "latest", expect=0)
    assert "alpha-token" in out and "bravo-key" in out
    rc, out, _ = run(capsys, "verify", expect=0)
    rc, out, _ = run(capsys, "verify", "--deep", "--code-file", cf, expect=0)
    assert "decrypted and compared" in out
    vault.remove("alpha-token")
    rc, out, _ = run(capsys, "restore", "latest", "--code-file", cf, expect=0)
    assert "DRY RUN" in out and "add" in out and not vault.exists("alpha-token")
    assert "restore" in out.split("Next:")[1]
    rc, out, _ = run(capsys, "restore", "latest", "--apply", "--code-file", cf, expect=0)
    assert "RESTORED" in out and vault.exists("alpha-token")
    rc, out, _ = run(capsys, "prune", expect=0)
    assert "would prune" in out
    rc, out, _ = run(capsys, "prune", "--apply", expect=0)


def test_json_output_has_no_values(capsys, state_dir, vault, key_via_cli):
    rc, out, err = run(capsys, "backup", "--json", expect=0)
    doc = json.loads(out)
    assert doc["ok"] and doc["value"]["count"] == 2
    assert CANARY_A not in out + err and CANARY_B not in out + err


def test_restore_without_a_code_and_no_terminal_explains_instead_of_hanging(capsys, state_dir, vault, key_via_cli):
    run(capsys, "backup", expect=0)
    rc, out, err = run(capsys, "restore", "latest")
    assert rc == 1 and "recovery code" in err and "--code-file" in err


def test_backup_with_no_key_says_how_to_fix_it(capsys, state_dir, vault, fast_kdf):
    rc, out, err = run(capsys, "backup")
    assert rc == 1 and "key-init" in out + err and "Nothing was written" in err


def test_key_init_refuses_to_replace_without_force_and_has_nowhere_to_show_the_code(capsys, tmp_path, state_dir,
                                                                                    fast_kdf, key_via_cli):
    rc, out, err = run(capsys, "key-init", "--code-out", str(tmp_path / "again.txt"))
    assert rc == 1 and "already exists" in err
    assert not (tmp_path / "again.txt").exists()


def test_key_init_without_a_place_to_show_the_code_creates_nothing(state_dir, fast_kdf):
    """The skill never mints a code it cannot show: no reveal sink and no passphrase -> nothing created."""
    from axiom.extensions.builtins.vault import skills
    from axiom.extensions.builtins.vault.cli import _build_ctx
    from axiom.infra.skill_dispatch import invoke_capability

    res = invoke_capability(skills.bind_default(), "vault.key_init", {}, _build_ctx(), surface="cli")
    assert not res.ok and vb.load_recipient(state_dir) is None


def test_code_out_file_is_new_and_private(capsys, tmp_path, state_dir, fast_kdf):
    out_file = tmp_path / "c.txt"
    run(capsys, "key-init", "--code-out", str(out_file), expect=0)
    assert oct(out_file.stat().st_mode & 0o777) == "0o600"


def test_restore_flags_conflicts_and_overwrite_flow(capsys, tmp_path, state_dir, vault, key_via_cli):
    cf = code_file(tmp_path, key_via_cli[0])
    run(capsys, "backup", expect=0)
    vault.set("bravo-key", b"changed-value-9")
    rc, out, _ = run(capsys, "restore", "latest", "--code-file", cf, expect=0)
    assert "conflict" in out and "--overwrite" in out
    rc, out, _ = run(capsys, "restore", "latest", "--apply", "--code-file", cf, expect=0)
    with vault.get("bravo-key") as s:
        assert s.value == b"changed-value-9"
    rc, out, _ = run(capsys, "restore", "latest", "--apply", "--overwrite", "--code-file", cf, expect=0)
    assert "before changing anything" in out
    with vault.get("bravo-key") as s:
        assert s.value == CANARY_B.encode()


def test_exposed_marks_backups_and_scrub_purges_them(capsys, tmp_path, state_dir, vault, key_via_cli, monkeypatch):
    """`secrets exposed` taints; `vault scrub` (with the code) purges."""
    from axiom.extensions.builtins.secrets.skills import exposed
    from axiom.infra.skills import SkillContext, default_registry

    cf = code_file(tmp_path, key_via_cli[0])
    run(capsys, "backup", expect=0)

    class Provider:
        def rotate(self, current, expires_at=None):
            class M:
                new_value = "freshly-minted-value-0001"
                expires_at = None
                handle = "h"

            return M()

        def probe(self, v):
            return True, "ok"

    ctx = SkillContext(registry=default_registry(), state_dir=state_dir, logger=logging.getLogger("t"))
    res = exposed.run({"ref": "alpha-token", "where": "transcript", "_rotation_provider": Provider()}, ctx)
    assert res.ok, res.errors
    assert any("vault backup(s) hold the exposed value" in a for a in res.actions_taken)
    assert any("vault scrub --name alpha-token" in a for a in res.actions_taken)
    # the post-rotation backup exists and is clean; the old one is tainted
    rc, out, _ = run(capsys, "ls", expect=0)
    assert "TAINTED" in out
    rc, out, _ = run(capsys, "scrub", "--name", "alpha-token", "--code-file", cf, expect=0)
    assert "re-encrypted 1 backup(s)" in out
    rc, out, _ = run(capsys, "ls", expect=0)
    assert "TAINTED" not in out
    rc, out, _ = run(capsys, "verify", "--deep", "--code-file", cf, expect=0)
    # the exposed value is in no archive any more
    root = state_dir / "vault-backups"
    for f in (root / "archives").glob("*.axvb"):
        _, body = C.open_archive(f.read_bytes(), unlock=key_via_cli[0])
        assert all(e["value"] != CANARY_A.encode() for e in body["entries"].values())


# -- the hooks -------------------------------------------------------------------------


def test_set_and_rm_trigger_a_debounced_backup_only_when_a_key_exists(state_dir, fast_kdf):
    from axiom.extensions.builtins.secrets.skills import rm
    from axiom.extensions.builtins.secrets.skills import set as set_skill
    from axiom.infra.skills import SkillContext, default_registry

    ctx = SkillContext(registry=default_registry(), state_dir=state_dir, logger=logging.getLogger("t"))
    r = set_skill.run({"name": "k1", "value": "some-throwaway-value-1", "expires_at": "2099-01-01"}, ctx)
    assert r.ok and any("not covered by a vault backup" in a for a in r.actions_taken)
    assert vb.list_backups(state_dir / "vault-backups") == []
    vb.init_key(state_dir)
    r = set_skill.run({"name": "k2", "value": "another-throwaway-value-2", "expires_at": "2099-01-01"}, ctx)
    assert r.ok and not any("not covered" in a for a in r.actions_taken)
    assert len(vb.list_backups(state_dir / "vault-backups")) == 1
    r = rm.run({"name": "k1", "yes": True}, ctx)
    assert r.ok  # debounced: deferred, not a second backup
    assert len(vb.list_backups(state_dir / "vault-backups")) == 1


def test_a_failing_backup_never_fails_the_credential_command(state_dir, fast_kdf, monkeypatch):
    from axiom.extensions.builtins.secrets.skills import set as set_skill
    from axiom.infra.skills import SkillContext, default_registry

    vb.init_key(state_dir)
    monkeypatch.setattr(vb.engine, "create_backup", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    ctx = SkillContext(registry=default_registry(), state_dir=state_dir, logger=logging.getLogger("t"))
    r = set_skill.run({"name": "k3", "value": "throwaway-value-3333", "expires_at": "2099-01-01"}, ctx)
    assert r.ok and any("did not run" in a for a in r.actions_taken)


def test_state_snapshots_exclude_the_vault_backup_files(state_dir):
    from axiom.infra import state_snapshots as ss

    for rel in ("vault-backups/archives/vb-1.axvb", "vault-backups/ledger.jsonl",
                "secrets/vault-backup-recipient.json", "secrets/vault-backup-head.json"):
        assert ss.classify(rel).action == "exclude", rel
    assert ss.classify("secrets/foreign-credentials.json").action == "include"


# -- canaries --------------------------------------------------------------------------------


def _forms(secret: str) -> list[bytes]:
    raw = secret.encode()
    return [raw, base64.b64encode(raw), base64.b64encode(raw).rstrip(b"="), raw.hex().encode()]


def _scan(paths, needles, skip=()):
    hits = []
    for base in paths:
        for f in ([base] if base.is_file() else base.rglob("*")):
            if not f.is_file() or f in skip:
                continue
            blob = f.read_bytes()
            for n in needles:
                for form in _forms(n):
                    if form in blob:
                        hits.append((str(f), n[:12]))
    return hits


def test_no_canary_value_or_code_leaks_anywhere(capsys, caplog, tmp_path, state_dir, home_dir, fast_kdf):
    caplog.set_level(logging.DEBUG)
    transcript: list[str] = []

    def go(*argv, expect=None):
        rc, out, err = run(capsys, *argv, expect=expect)
        transcript.extend([out, err])
        return rc, out, err

    # real credential commands, driven through the real skills
    from axiom.extensions.builtins.secrets.skills import exposed, rm
    from axiom.extensions.builtins.secrets.skills import set as set_skill
    from axiom.infra.skills import SkillContext, default_registry

    ctx = SkillContext(registry=default_registry(), state_dir=state_dir, logger=logging.getLogger("canary"))
    code_path = tmp_path / "recovery-code.txt"
    go("key-init", "--code-out", str(code_path), expect=0)
    code = code_path.read_text().strip()
    cf = code_file(tmp_path, code)
    for nm, val in (("alpha-token", CANARY_A), ("bravo-key", CANARY_B)):
        res = set_skill.run({"name": nm, "value": val, "expires_at": "2099-01-01"}, ctx)
        transcript += [json.dumps(res.value, default=str), *res.actions_taken, *res.errors]
    (state_dir / "identity").mkdir()
    (state_dir / "identity" / "private.pem").write_text(CANARY_FILE)
    key_file = state_dir / "identity" / "private.pem"

    go("backup", "--force", expect=0)
    go("ls", expect=0)
    go("show", "latest", expect=0)
    go("verify", expect=0)
    go("verify", "--deep", "--code-file", cf, expect=0)
    go("restore", "latest", "--code-file", cf, expect=0)
    go("prune", expect=0)
    # failures: wrong code, mistyped code, missing archive
    wrong = code_file(tmp_path, C.generate_recovery_code())
    rc, out, err = go("restore", "latest", "--apply", "--code-file", wrong)
    assert rc == 1
    with pytest.raises(C.DecryptionError) as exc:
        vb.restore(state_dir, state_dir / "vault-backups", "latest", unlock="AAAA", apply=True)
    transcript.append(str(exc.value))
    rm_res = rm.run({"name": "alpha-token", "yes": True}, ctx)
    transcript += [json.dumps(rm_res.value, default=str), *rm_res.actions_taken]
    go("restore", "latest", "--apply", "--only", "alpha-token", "--code-file", cf, expect=0)

    class P:
        def rotate(self, current, expires_at=None):
            class M:
                new_value = "freshly-minted-CANARY-value"
                expires_at = None
                handle = "h"

            return M()

        def probe(self, v):
            return True, "ok"

    ex = exposed.run({"ref": "bravo-key", "where": "transcript", "_rotation_provider": P()}, ctx)
    transcript += [json.dumps(ex.value, default=str), *ex.actions_taken, *ex.errors]
    go("scrub", "--name", "bravo-key", "--code-file", cf, expect=0)
    go("verify", "--deep", "--code-file", cf, expect=0)
    go("key-info", expect=0)
    go("prune", "--apply", expect=0)

    secrets_ = [CANARY_A, CANARY_B, CANARY_FILE, "freshly-minted-CANARY-value"]
    text = "\n".join(transcript) + "\n" + caplog.text
    for s in secrets_ + [code]:
        assert s not in text, f"a secret reached output/logs: {s[:12]}"
    # the recovery code exists in exactly the file the person asked for, and the key file is the
    # real source of CANARY_FILE; the vault store is the (sandboxed) custody backend for the rest.
    custody = tmp_path / "file-secrets.json"
    hits = _scan([state_dir, home_dir], secrets_ + [code], skip={key_file, code_path})
    # the file-backend custody lives outside state_dir; state_dir must hold no value of any kind
    assert hits == [], hits
    assert custody.exists()
    # cleartext manifests / headers / ledger carry names + fingerprints only
    for f in (state_dir / "vault-backups" / "archives").glob("*.axvb"):
        header, _, _ = C.read_header(f.read_bytes())
        assert all("value" not in m for m in header["entries"])  # no `value` field in any manifest row
    # and nothing weaker than owner-only is on disk
    for f in (state_dir / "vault-backups").rglob("*"):
        mode = f.stat().st_mode & 0o777
        assert mode in (0o600, 0o700), (f, oct(mode))


def test_exception_messages_never_contain_values(state_dir, vault, root, keys):
    r = vb.create_backup(state_dir)
    msgs = []
    for bad in (lambda: C.open_archive(Path(r.path).read_bytes(), unlock="wrong-wrong-wrong-1"),
                lambda: C.open_archive(b"junk", unlock="x"),
                lambda: vb.restore(state_dir, root, "nope", unlock="x")):
        try:
            bad()
        except Exception as e:  # noqa: BLE001
            msgs.append(str(e) + repr(e))
    assert msgs and not any(c in m for m in msgs for c in (CANARY_A, CANARY_B, keys.code))


def test_the_audit_trail_records_names_only(capsys, tmp_path, state_dir, vault, key_via_cli):
    cf = code_file(tmp_path, key_via_cli[0])
    run(capsys, "backup", expect=0)
    vault.remove("alpha-token")
    run(capsys, "restore", "latest", "--apply", "--code-file", cf, expect=0)
    trail = list((state_dir / "audit").rglob("*.jsonl"))
    assert trail, "restore must leave an audit entry"
    blob = "\n".join(p.read_text() for p in trail)
    assert "vault.backup.restore" in blob and "alpha-token" in blob
    assert CANARY_A not in blob and key_via_cli[0] not in blob


def test_unexpected_exceptions_are_reduced_to_their_type(capsys, state_dir, vault, key_via_cli, monkeypatch):
    """A backend's exception text is not ours to vouch for: only its type name crosses the skill boundary."""
    monkeypatch.setattr(vb, "create_backup", lambda *a, **k: (_ for _ in ()).throw(ValueError(CANARY_A)))
    rc, out, err = run(capsys, "backup")
    assert rc == 1 and "ValueError" in err and CANARY_A not in out + err
