# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Stale credential holders — processes a rotation left behind.

The incident: a chat backend ran for three weeks holding a pre-rotation
database password. The credential on disk was correct the whole time, the unit
reported ``active``, and every grounded question failed politely.
"""

from __future__ import annotations


from axiom.extensions.builtins.secrets.discovery import (
    ProcessEnvProbe,
    RawHit,
    correlate_stale,
    fingerprint,
)

NEW = "glpat-" + "N3w0000000000000000a"
OLD = "glpat-" + "O1d0000000000000000b"


def _disk(key, value, path="/etc/app.env"):
    return RawHit(locator=f"{path}:{key}", value=value, probe="env-file")


def _proc(pid, comm, key, value):
    return RawHit(locator=f"pid {pid} ({comm}) {key}", value=value, probe="process-env")


class TestCorrelation:
    def test_a_process_holding_a_superseded_value_is_stale(self):
        stale = correlate_stale([_disk("DB_PASS", NEW), _proc("42", "svc", "DB_PASS", OLD)])
        assert len(stale) == 1
        s = stale[0]
        assert s.pid == "42" and s.process == "svc" and s.key == "DB_PASS"
        assert s.held_fingerprint == fingerprint(OLD)
        assert s.disk_fingerprint == fingerprint(NEW)
        assert "restart" in s.remediation

    def test_a_process_in_step_with_disk_is_not_reported(self):
        assert correlate_stale([_disk("DB_PASS", NEW), _proc("42", "svc", "DB_PASS", NEW)]) == []

    def test_a_variable_no_file_supplies_is_not_evidence(self):
        """It may have been passed directly. Guessing here is how a check earns
        a reputation for false positives and stops being read."""
        assert correlate_stale([_proc("42", "svc", "MYSTERY", OLD)]) == []

    def test_values_never_appear_in_the_report(self):
        s = correlate_stale([_disk("DB_PASS", NEW), _proc("42", "svc", "DB_PASS", OLD)])[0]
        blob = repr(s.to_dict())
        assert NEW not in blob and OLD not in blob

    def test_one_finding_per_process_and_key(self):
        hits = [_disk("DB_PASS", NEW)] + [_proc("42", "svc", "DB_PASS", OLD)] * 3
        assert len(correlate_stale(hits)) == 1

    def test_several_processes_on_one_stale_variable_all_report(self):
        stale = correlate_stale([
            _disk("DB_PASS", NEW),
            _proc("1", "a", "DB_PASS", OLD),
            _proc("2", "b", "DB_PASS", OLD),
        ])
        assert {s.pid for s in stale} == {"1", "2"}


class TestProcessEnvProbe:
    def test_reads_credential_material_from_a_process_environ(self, tmp_path):
        proc = tmp_path / "proc"
        (proc / "4242").mkdir(parents=True)
        (proc / "4242" / "environ").write_bytes(
            b"PATH=/usr/bin\0DB_PASS=" + NEW.encode() + b"\0"
        )
        (proc / "4242" / "comm").write_text("fused-rag\n")
        hits = list(ProcessEnvProbe(proc_root=proc).scan(tmp_path))
        assert len(hits) == 1
        assert hits[0].locator == "pid 4242 (fused-rag) DB_PASS"
        assert hits[0].value == NEW

    def test_unreadable_processes_are_skipped_not_fatal(self, tmp_path):
        """Most of /proc belongs to other users. A sweep that dies on the first
        EPERM inventories nothing."""
        proc = tmp_path / "proc"
        (proc / "1").mkdir(parents=True)  # no environ file at all
        (proc / "77").mkdir(parents=True)
        (proc / "77" / "environ").write_bytes(b"T=" + NEW.encode() + b"\0")
        (proc / "77" / "comm").write_text("ok\n")
        hits = list(ProcessEnvProbe(proc_root=proc).scan(tmp_path))
        assert [h.locator for h in hits] == ["pid 77 (ok) T"]

    def test_non_linux_yields_nothing_rather_than_failing(self, tmp_path):
        assert list(ProcessEnvProbe(proc_root=tmp_path / "nope").scan(tmp_path)) == []

    def test_non_credential_env_vars_are_ignored(self, tmp_path):
        proc = tmp_path / "proc"
        (proc / "5").mkdir(parents=True)
        (proc / "5" / "environ").write_bytes(b"HOME=/root\0LANG=C.UTF-8\0")
        (proc / "5" / "comm").write_text("x\n")
        assert list(ProcessEnvProbe(proc_root=proc).scan(tmp_path)) == []


class TestTargetCorrelation:
    """The real incident: one database, two variable names."""

    OLD_DSN = "postgresql://triga_ro:oldpassword123@localhost:5432/axiom_db"
    NEW_DSN = "postgresql://triga_ro:newpassword456@localhost:5432/axiom_db"

    def _hits(self):
        from axiom.extensions.builtins.secrets.discovery.model import credential_target
        return [
            RawHit(locator="/cfg/axiom-serve.env:TRIGA_TELEMETRY_DSN",
                   value=self.NEW_DSN, probe="env-file",
                   target=credential_target(self.NEW_DSN)),
            RawHit(locator="pid 4242 (fused-rag) RAG_DB_URL",
                   value=self.OLD_DSN, probe="process-env",
                   target=credential_target(self.OLD_DSN)),
        ]

    def test_correlates_across_different_variable_names(self):
        """Name-only correlation misses this — and this is the outage that
        actually happened."""
        stale = correlate_stale(self._hits())
        assert len(stale) == 1
        assert stale[0].process == "fused-rag"
        assert stale[0].key == "RAG_DB_URL"

    def test_different_databases_are_not_conflated(self):
        from axiom.extensions.builtins.secrets.discovery.model import credential_target
        other = "postgresql://triga_ro:pw@otherhost:5432/axiom_db"
        stale = correlate_stale([
            RawHit(locator="/cfg/a.env:A_DSN", value=self.NEW_DSN, probe="env-file",
                   target=credential_target(self.NEW_DSN)),
            RawHit(locator="pid 7 (x) B_DSN", value=other, probe="process-env",
                   target=credential_target(other)),
        ])
        assert stale == [], "a different host is a different credential"

    def test_same_target_same_secret_is_healthy(self):
        from axiom.extensions.builtins.secrets.discovery.model import credential_target
        assert correlate_stale([
            RawHit(locator="/cfg/a.env:A_DSN", value=self.NEW_DSN, probe="env-file",
                   target=credential_target(self.NEW_DSN)),
            RawHit(locator="pid 7 (x) B_DSN", value=self.NEW_DSN, probe="process-env",
                   target=credential_target(self.NEW_DSN)),
        ]) == []
