# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Named invariants behind the chaos matrix, in-process and fast.

The matrix proves outcomes through subprocesses. These pin the *reasons*: the
shared helper's contract, and the handful of rules that must never bend
(damage never widens a grant, never mints a replacement identity, never
overwrites what it could not read).
"""

from __future__ import annotations

import json
import os

import pytest

from axiom.infra import state_integrity as si
from axiom.infra.state import locked_append_jsonl


@pytest.fixture(autouse=True)
def _never_touch_real_state(tmp_path, monkeypatch):
    """Every test here runs against temp state, including the credential VALUE store.

    ``ForeignCredentialStore(tmp_path)`` isolates only the metadata index; the
    values go to ``$AXI_STATE_DIR/secrets/foreign-values.json``, which without
    this is the developer's real ``~/.axi``.
    """
    iso = tmp_path / "iso-state"
    iso.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(iso))
    monkeypatch.setenv("AXIOM_STATE_DIR", str(iso))
    monkeypatch.setenv("AXIOM_FILE_SECRETS_PATH", str(tmp_path / "values.json"))
    monkeypatch.setenv("AXIOM_FOREIGN_SECRETS_BACKEND", "file")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert str(iso).startswith(str(tmp_path))


@pytest.fixture
def state(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(d))
    monkeypatch.setenv("AXIOM_STATE_DIR", str(d))
    return d


class TestQuarantine:
    def test_moves_aside_and_never_deletes(self, tmp_path):
        f = tmp_path / "a.json"
        f.write_text("precious")
        moved = si.quarantine(f)
        assert moved is not None and moved.read_text() == "precious"
        assert not f.exists()
        assert ".corrupt-" in moved.name

    def test_two_quarantines_in_one_second_do_not_collide(self, tmp_path):
        names = set()
        for i in range(3):
            f = tmp_path / "a.json"
            f.write_text(str(i))
            names.add(si.quarantine(f))
        assert len(names) == 3

    def test_a_directory_is_quarantined_whole(self, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        (d / "x").write_text("1")
        moved = si.quarantine(d)
        assert (moved / "x").read_text() == "1"


class TestAtomicWrite:
    def test_a_failed_replace_leaves_the_original_and_no_temp_file(self, tmp_path, monkeypatch):
        f = tmp_path / "a.txt"
        f.write_text("old")

        def boom(*a, **k):
            raise OSError("disk full")

        with monkeypatch.context() as m:
            m.setattr(os, "replace", boom)
            with pytest.raises(OSError):
                si.atomic_write_text(f, "new")
        assert f.read_text() == "old"
        assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]

    def test_mode_is_applied_before_the_file_is_visible(self, tmp_path):
        f = tmp_path / "k"
        si.atomic_write_bytes(f, b"secret", mode=0o600)
        assert (f.stat().st_mode & 0o777) == 0o600


class TestLoadJsonState:
    def test_absent_is_the_default_and_silent(self, tmp_path, caplog):
        assert si.load_json_state(tmp_path / "no.json", default={"d": 1}) == {"d": 1}
        assert not caplog.records

    def test_damaged_nonsecurity_is_quarantined_and_warned(self, tmp_path, caplog):
        f = tmp_path / "p.json"
        f.write_text("{oops")
        with caplog.at_level("WARNING"):
            out = si.load_json_state(f, default={})
        assert out == {}
        assert not f.exists() and list(tmp_path.glob("p.json.corrupt-*"))
        assert "p.json" in caplog.text

    def test_damaged_failclosed_raises_with_a_fix_and_leaves_the_file(self, tmp_path):
        f = tmp_path / "grant.json"
        f.write_text("{oops")
        with pytest.raises(si.StateDamaged) as err:
            si.load_json_state(f, fail_closed=True)
        assert str(f) in str(err.value) and "Fix:" in str(err.value)
        assert f.read_text() == "{oops"

    @pytest.mark.parametrize("body", ["", "   ", "[1, 2]", '"s"', "\x00\x01"])
    def test_every_unusable_body_is_damage_not_empty(self, tmp_path, body):
        f = tmp_path / "g.json"
        f.write_text(body)
        if body.strip() and body.strip().startswith(("{",)):
            pytest.skip("valid")
        with pytest.raises(si.StateDamaged):
            si.load_json_state(f, expect=dict, fail_closed=True)

    def test_a_directory_in_place_of_the_file_is_damage(self, tmp_path):
        f = tmp_path / "g.json"
        f.mkdir()
        with pytest.raises(si.StateDamaged, match="directory"):
            si.load_json_state(f, fail_closed=True)

    def test_a_dangling_symlink_is_damage_not_absence(self, tmp_path):
        f = tmp_path / "g.json"
        os.symlink(tmp_path / "nowhere", f)
        with pytest.raises(si.StateDamaged, match="does not exist"):
            si.load_json_state(f, fail_closed=True)


class TestTheSnapshotSeam:
    def test_with_no_snapshot_the_seam_answers_false_and_never_raises(self, tmp_path):
        assert si.restore_if_snapshot(tmp_path / "anything") is False

    def test_a_snapshot_restores_a_missing_file_and_says_so(self, tmp_path, monkeypatch, caplog):
        from pathlib import Path
        from types import SimpleNamespace

        target = tmp_path / "g.json"

        def fake_restore(path):
            Path(path).write_text('{"restored": true}')
            return SimpleNamespace(snapshot_id="snap-1")

        monkeypatch.setattr("axiom.infra.state_snapshots.restore_file", fake_restore)
        with caplog.at_level("WARNING"):
            out = si.load_json_state(target, default={})
        assert out == {"restored": True}
        assert "restored from snapshot" in caplog.text

    def test_a_failing_snapshot_store_is_not_a_second_failure(self, tmp_path, monkeypatch):
        def broken(path):
            raise OSError("store unreadable")

        monkeypatch.setattr("axiom.infra.state_snapshots.restore_file", broken)
        assert si.restore_if_snapshot(tmp_path / "g.json") is False


class TestAppendHealsAFragment:
    def test_a_record_after_a_cut_short_line_is_not_lost(self, tmp_path):
        f = tmp_path / "log.jsonl"
        locked_append_jsonl(f, {"n": 1})
        with f.open("ab") as fh:
            fh.write(b'{"n": 2, "tru')  # writer killed mid-line
        locked_append_jsonl(f, {"n": 3})
        good = []
        for line in f.read_text().splitlines():
            try:
                good.append(json.loads(line)["n"])
            except ValueError:
                pass
        assert good == [1, 3]


class TestConsentNeverWidens:
    @pytest.mark.parametrize("body", ["{oops", "", '["x"]', "\x00\xff"])
    def test_damage_enables_nothing(self, state, body):
        from axiom.extensions.builtins.agents import consent as C

        C.record_decision(enabled=[], opted_out=True)
        C.consent_path().write_text(body)
        c = C.load_consent()
        assert c.enabled == [] and c.opted_out is True


class TestIdentityIsNeverReplaced:
    def test_a_second_generate_refuses_to_overwrite_the_key(self, tmp_path):
        from axiom.vega.federation.identity import IdentityDamaged, generate_identity

        first = generate_identity("a@b", keys_dir=tmp_path)
        before = (tmp_path / "private.pem").read_bytes()
        with pytest.raises(IdentityDamaged, match="private.pem"):
            generate_identity("a@b", keys_dir=tmp_path)
        assert (tmp_path / "private.pem").read_bytes() == before
        assert first.node_id

    def test_replace_is_an_explicit_act(self, tmp_path):
        from axiom.vega.federation.identity import generate_identity

        a = generate_identity("a@b", keys_dir=tmp_path)
        b = generate_identity("a@b", keys_dir=tmp_path, replace=True)
        assert a.node_id != b.node_id

    def test_missing_identity_json_beside_a_key_is_damage_not_none(self, tmp_path):
        from axiom.vega.federation.identity import IdentityDamaged, generate_identity, load_identity

        generate_identity("a@b", keys_dir=tmp_path)
        (tmp_path / "identity.json").unlink()
        with pytest.raises(IdentityDamaged, match="identity.json"):
            load_identity(tmp_path)

    def test_a_clean_slate_is_none(self, tmp_path):
        from axiom.vega.federation.identity import load_identity

        assert load_identity(tmp_path) is None


class TestTheScopeKeyIsNeverReminted:
    def test_fresh_scope_gets_a_key_with_private_mode(self, tmp_path):
        from axiom.memory.keyfile import load_or_create_scope_key

        load_or_create_scope_key(tmp_path)
        assert ((tmp_path / "node.key").stat().st_mode & 0o777) == 0o600

    def test_same_key_comes_back(self, tmp_path):
        from axiom.memory.keyfile import load_or_create_scope_key

        a = load_or_create_scope_key(tmp_path)
        b = load_or_create_scope_key(tmp_path)
        assert a.public_bytes == b.public_bytes

    def test_missing_key_with_signed_history_is_refused(self, tmp_path):
        from axiom.memory.keyfile import load_or_create_scope_key

        load_or_create_scope_key(tmp_path)
        (tmp_path / "audit.jsonl").write_text('{"signature": "ab"}\n')
        (tmp_path / "node.key").unlink()
        with pytest.raises(si.StateDamaged, match="audit.jsonl"):
            load_or_create_scope_key(tmp_path)
        assert not (tmp_path / "node.key").exists()


class TestSettingsFailRestrictive:
    def test_damage_reads_as_the_restrictive_values_and_refuses_to_overwrite(self, state):
        from axiom.extensions.builtins.settings.store import SettingsStore

        p = state / "settings.toml"
        p.write_text("[raci]\ntrust = 4\n")
        p.write_text("this is = = not toml [")
        s = SettingsStore()
        assert s.get("raci.trust") == 1
        assert s.get("autonomy.enabled") is False
        with pytest.raises(si.StateDamaged):
            s.set("user.name", "x", scope="global")
        assert p.read_text() == "this is = = not toml ["


class TestSiteRulesStayFailClosed:
    def test_zero_bytes_is_not_an_empty_policy(self, tmp_path):
        from axiom.infra.authority_rules import SiteRuleError, load_site_policy

        p = tmp_path / "site_rules.toml"
        p.write_text("")
        with pytest.raises(SiteRuleError, match="empty"):
            load_site_policy(p)

    def test_a_dangling_link_is_present_and_unloadable(self, tmp_path):
        from axiom.infra.authority_rules import SiteRuleError, load_site_policy

        p = tmp_path / "site_rules.toml"
        os.symlink(tmp_path / "gone", p)
        with pytest.raises(SiteRuleError, match="does not exist"):
            load_site_policy(p)


class TestVaultIndexIsNeverOverwrittenFromNothing:
    def test_a_damaged_index_blocks_set_and_keeps_its_bytes(self, tmp_path):
        from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

        s = ForeignCredentialStore(tmp_path)
        s.set("alpha", b"x")
        idx = s.index_path
        idx.write_text('{"credentials": {"alpha": ')  # cut short
        with pytest.raises(si.StateDamaged, match="foreign-credentials.json"):
            s.set("beta", b"y")
        assert idx.read_text() == '{"credentials": {"alpha": '

    def test_the_index_is_mode_600_after_an_atomic_write(self, tmp_path):
        from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

        s = ForeignCredentialStore(tmp_path)
        s.set("alpha", b"x")
        assert (s.index_path.stat().st_mode & 0o777) == 0o600
