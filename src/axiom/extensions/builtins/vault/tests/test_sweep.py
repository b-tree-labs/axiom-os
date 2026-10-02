# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""KEEP's stewardship sweep — the KEEP/secrets boundary, and escalation."""

from __future__ import annotations

from axiom.extensions.builtins.vault.skills import sweep, verbs

GLPAT = "glpat-" + "Z9y8X7w6V5u4T3s2R1q0"


class TestSweep:
    def test_clean_tree_is_ok(self, tmp_path):
        (tmp_path / "a.env").write_text("PLAIN=nothing\n")
        r = sweep.run({"workspace": tmp_path, "env_root": tmp_path, "home": tmp_path})
        assert r.ok is True

    def test_unmanaged_material_fails_the_sweep(self, tmp_path):
        """Escalation, not reporting: a heartbeat that finds a loose credential
        must be visibly not-ok, or it becomes a line in a log nobody reads."""
        (tmp_path / "a.env").write_text(f"TOKEN={GLPAT}\n")
        r = sweep.run({
            "workspace": tmp_path, "env_root": tmp_path, "home": tmp_path,
            "known_fingerprints": ["something-else"],
        })
        assert r.ok is False
        assert r.value["unmanaged"] == 1
        assert any("outside the store" in e for e in r.errors)
        assert any("unmanaged:" in e for e in r.errors)

    def test_material_already_in_the_store_is_inventory_not_a_finding(self, tmp_path):
        from axiom.extensions.builtins.secrets.discovery import fingerprint

        (tmp_path / "a.env").write_text(f"TOKEN={GLPAT}\n")
        r = sweep.run({
            "workspace": tmp_path, "env_root": tmp_path, "home": tmp_path,
            "known_fingerprints": [fingerprint(GLPAT)],
        })
        assert r.ok is True, "a known credential in a known place is not a leak"

    def test_unchecked_findings_are_reported_but_do_not_fail(self, tmp_path):
        """Without a store index we cannot claim anything is unmanaged — saying
        so anyway would make the sweep cry wolf on every run."""
        (tmp_path / "a.env").write_text(f"TOKEN={GLPAT}\n")
        r = sweep.run({"workspace": tmp_path, "env_root": tmp_path, "home": tmp_path})
        assert r.ok is True
        assert any("unchecked" in a for a in r.actions_taken)

    def test_keep_delegates_discovery_rather_than_reimplementing_it(self):
        """The ADR-001 boundary: KEEP owns the policy, `secrets` owns the
        mechanism. If KEEP ever grows its own scanner, this test should be the
        thing that objects."""
        import inspect

        src = inspect.getsource(sweep)
        assert "secrets.skills" in src or "discover_skill" in src
        assert "glob" not in src and "re.compile" not in src

    def test_sweep_is_a_registered_verb(self):
        assert "sweep" in verbs()
