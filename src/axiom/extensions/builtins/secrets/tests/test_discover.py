# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.discover`` — finding credential material outside the store.

Generalisation is the property under test: a new *location* and a new *vendor*
must be independent registrations, and neither may require touching the scanner.
"""

from __future__ import annotations

import subprocess

import pytest

from axiom.extensions.builtins.secrets.discovery import (
    fingerprint,
    match_text,
    register_matcher,
    register_probe,
    run_probes,
)
from axiom.extensions.builtins.secrets.discovery.model import RawHit
from axiom.extensions.builtins.secrets.skills.discover import (
    discover_findings,
)
from axiom.extensions.builtins.secrets.skills.discover import (
    run as discover,
)

GLPAT = "glpat-" + "A1b2C3d4E5f6G7h8J9k0"
GHPAT = "ghp_" + "a" * 36


@pytest.fixture(autouse=True)
def _isolate_registries():
    """Snapshot and restore the probe/matcher registries.

    These tests register probes and matchers to prove extensibility, and both
    registries are module-global. Without this, a fake probe registered here
    leaks into every later test in the session — which is precisely the
    ambient-state coupling this whole sweep exists to detect.
    """
    from axiom.extensions.builtins.secrets.discovery import matchers, probes

    saved_p = dict(probes.PROBES)
    saved_m = dict(matchers.MATCHERS)
    yield
    probes.PROBES.clear()
    probes.PROBES.update(saved_p)
    matchers.MATCHERS.clear()
    matchers.MATCHERS.update(saved_m)


class TestMatchers:
    def test_recognises_vendor_prefixes(self):
        assert ("gitlab-pat", GLPAT) in match_text(f"https://oauth2:{GLPAT}@host/x.git")

    def test_most_specific_matcher_wins(self):
        """A URL-embedded GitLab token matches both 'url-userinfo' and
        'gitlab-pat'. Reporting the vendor is actionable; reporting the shape
        only says something is wrong."""
        hits = dict((v, n) for n, v in match_text(f"https://oauth2:{GLPAT}@host/x"))
        assert hits[GLPAT] == "gitlab-pat"

    def test_a_new_vendor_is_a_registration_not_a_code_change(self):
        register_matcher("acme-token", r"acme_[0-9a-f]{12}", specificity=80)
        assert ("acme-token", "acme_0123456789ab") in match_text("k=acme_0123456789ab")

    def test_unknown_vendors_are_still_caught_by_shape(self):
        """The point of the heuristic matchers: a vendor nobody registered."""
        names = [n for n, _ in match_text("https://user:sup3rsecret999@example.com/x")]
        assert "url-userinfo" in names

    def test_private_keys_are_caught(self):
        assert any(
            n == "private-key-block"
            for n, _ in match_text("-----BEGIN OPENSSH PRIVATE KEY-----")
        )


class TestProbes:
    def test_git_remote_probe_finds_an_embedded_token(self, tmp_path):
        repo = tmp_path / "myrepo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin",
             f"https://oauth2:{GLPAT}@example.com/g/p.git"], check=True)
        hits = run_probes({"git-remote": tmp_path}, only=["git-remote"])
        assert [h.locator for h in hits] == ["myrepo:remote/origin"]
        assert hits[0].value == GLPAT

    def test_env_file_probe_skips_commented_examples(self, tmp_path):
        """A documented placeholder in an .env.example is not a leak — flagging
        it teaches people to ignore the report."""
        (tmp_path / "a.env").write_text(
            f"# TOKEN={GHPAT}\nREAL_TOKEN={GHPAT}\n"
        )
        hits = run_probes({"env-file": tmp_path}, only=["env-file"])
        assert len(hits) == 1
        assert "REAL_TOKEN" in hits[0].locator

    def test_git_credentials_probe_reports_file_mode(self, tmp_path):
        p = tmp_path / ".git-credentials"
        p.write_text(f"https://oauth2:{GLPAT}@example.com\n")
        p.chmod(0o644)
        hits = run_probes({"git-credentials": tmp_path}, only=["git-credentials"])
        assert hits and "644" in hits[0].detail

    def test_a_failing_probe_does_not_abort_the_sweep(self, tmp_path):
        class Exploding:
            name = "exploding"

            def scan(self, root):
                raise RuntimeError("boom")

        register_probe(Exploding())
        (tmp_path / "a.env").write_text(f"T={GHPAT}\n")
        seen: list[str] = []
        hits = run_probes(
            {"default": tmp_path, "env-file": tmp_path},
            only=["exploding", "env-file"],
            on_error=lambda n, e: seen.append(n),
        )
        assert seen == ["exploding"]
        assert len(hits) == 1, "a partial inventory is useful; a crashed one is not"

    def test_a_new_location_is_a_registration_not_a_code_change(self, tmp_path):
        class Custom:
            name = "custom"

            def scan(self, root):
                yield RawHit(locator="somewhere", value=GLPAT, probe="custom")

        register_probe(Custom())
        hits = run_probes({"custom": tmp_path}, only=["custom"])
        assert [h.locator for h in hits] == ["somewhere"]


class TestFindings:
    def test_values_are_never_returned(self, tmp_path):
        """A discovery report must be safe to paste into a ticket."""
        (tmp_path / "a.env").write_text(f"T={GLPAT}\n")
        findings = discover_findings({"env-file": tmp_path}, only=["env-file"])
        blob = repr([f.to_dict() for f in findings])
        assert GLPAT not in blob
        assert findings[0].fingerprint == fingerprint(GLPAT)

    def test_unmanaged_when_the_fingerprint_is_absent_from_the_store(self, tmp_path):
        (tmp_path / "a.env").write_text(f"T={GLPAT}\n")
        f = discover_findings(
            {"env-file": tmp_path}, only=["env-file"], known_fingerprints={"deadbeef"}
        )[0]
        assert f.managed is False and f.severity == "unmanaged"
        assert any("adopt" in h for h in f.hints)

    def test_the_same_scan_answers_who_consumes_a_managed_credential(self, tmp_path):
        """Discovery and consumer-mapping are one mechanism, indexed differently."""
        (tmp_path / "a.env").write_text(f"T={GLPAT}\n")
        f = discover_findings(
            {"env-file": tmp_path},
            only=["env-file"],
            known_fingerprints={fingerprint(GLPAT)},
        )[0]
        assert f.managed is True and f.severity == "consumer"

    def test_unchecked_when_no_store_index_is_supplied(self, tmp_path):
        (tmp_path / "a.env").write_text(f"T={GLPAT}\n")
        f = discover_findings({"env-file": tmp_path}, only=["env-file"])[0]
        assert f.managed is None and f.severity == "unknown"

    def test_unmanaged_findings_sort_first(self, tmp_path):
        (tmp_path / "a.env").write_text(f"MANAGED={GLPAT}\nLOOSE={GHPAT}\n")
        findings = discover_findings(
            {"env-file": tmp_path}, only=["env-file"],
            known_fingerprints={fingerprint(GLPAT)},
        )
        assert findings[0].managed is False, "the report opens on what needs action"


class TestSkill:
    def test_clean_run_is_ok_and_loose_material_is_not(self, tmp_path):
        (tmp_path / "clean.env").write_text("PLAIN=not-a-secret\n")
        assert discover({"env_root": tmp_path, "home": tmp_path,
                         "workspace": tmp_path, "probes": ["env-file"]}).ok is True

        (tmp_path / "loose.env").write_text(f"T={GLPAT}\n")
        res = discover({"env_root": tmp_path, "home": tmp_path, "workspace": tmp_path,
                        "probes": ["env-file"], "known_fingerprints": ["nope"]})
        assert res.ok is False
        assert res.value["unmanaged"] == 1


# --- nested env files (regression: real credentials live one level down) ----


#: A value long enough to satisfy the ``gitlab-pat`` matcher
#: (``glpat-[A-Za-z0-9_\-]{20,}``) while never appearing contiguously in this
#: file. GitHub push-protection scans file bytes, so an assembled string does
#: not trip it — and a shorter sentinel would not exercise the matcher at all.
#: A realistic-looking literal here blocked the public mirror for a week; see
#: ``scripts/build_public_mirror.py``.
GITLAB_PAT_FIXTURE = "glpat-" + "SENTINEL" + "0" * 12

def test_env_probe_finds_credentials_in_a_subdirectory(tmp_path):
    """An env file nested under the scan root must be found.

    Regression: the probe globbed ``root.glob("*.env")`` — non-recursive — while
    the skill rooted it at ``~/.config``. Real deployments park credentials in
    ``~/.config/<app>/<name>.env``, one level below, so a mode-600 credential
    file sat undiscovered while ``discover`` reported ``unmanaged: 0``.
    """
    from axiom.extensions.builtins.secrets.discovery.probes import EnvFileProbe

    nested = tmp_path / "axiom"
    nested.mkdir()
    secret = nested / "audit-hmac-key.env"
    secret.write_text(f'export SOME_API_TOKEN="{GITLAB_PAT_FIXTURE}"\n')
    secret.chmod(0o600)

    hits = list(EnvFileProbe().scan(tmp_path))

    assert hits, "a credential one directory below the scan root must be found"
    assert any("audit-hmac-key.env" in h.locator for h in hits)


def test_env_probe_still_finds_credentials_at_the_root(tmp_path):
    """The recursive fix must not regress the flat case."""
    from axiom.extensions.builtins.secrets.discovery.probes import EnvFileProbe

    flat = tmp_path / "service.env"
    flat.write_text(f'TOKEN="{GITLAB_PAT_FIXTURE}"\n')

    hits = list(EnvFileProbe().scan(tmp_path))

    assert any("service.env" in h.locator for h in hits)


# --- self-generated secrets (no vendor prefix to key off) -------------------


def test_high_entropy_assignment_is_matched():
    """A self-generated secret has no vendor prefix, so only shape can find it.

    Regression: every matcher keyed off a vendor signature (glpat-, gh?_, AKIA,
    sk-, PEM). An HMAC key you generated yourself matched nothing, so it stayed
    invisible to `axi secrets discover` — and the credentials that go unmanaged
    are precisely the ones nobody issued you.
    """
    line = 'export AXIOM_AUDIT_HMAC_KEY="' + "a1" * 32 + '"'
    names = [n for n, _v in match_text(line)]
    assert "high-entropy-assignment" in names


def test_high_entropy_matches_other_secret_key_names():
    for var in ("AXIOM_OPS_LOG_HMAC_KEY", "DB_PASSWORD", "SERVICE_TOKEN", "APP_SECRET"):
        line = f'{var}={"b3" * 24}'
        names = [n for n, _v in match_text(line)]
        assert "high-entropy-assignment" in names, f"{var} should match"


def test_high_entropy_ignores_non_secret_assignments():
    """Length alone is not suspicion — the variable name has to claim it."""
    for line in (
        "AXIOM_STATE_DIR=/Users/someone/Library/Application Support/axiom/state",
        "PYTHONPATH=/very/long/path/that/goes/on/and/on/for/quite/a/while/src",
    ):
        names = [n for n, _v in match_text(line)]
        assert "high-entropy-assignment" not in names, line


def test_high_entropy_ignores_short_and_placeholder_values():
    for line in ("DB_PASSWORD=changeme", "API_TOKEN=<REDACTED>", "APP_SECRET=''"):
        names = [n for n, _v in match_text(line)]
        assert "high-entropy-assignment" not in names, line


def test_vendor_matcher_still_wins_over_shape():
    """A GitLab PAT in a secret-named var reports as gitlab-pat, not as shape."""
    line = f'CI_SECRET_TOKEN="{GLPAT}"'
    results = match_text(line)
    assert results[0][0] == "gitlab-pat", results
