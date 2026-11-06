# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The scanner has to recognise our own credentials, on our own platform.

Three gaps, found by asking why a live `ANTHROPIC_API_KEY` sat in an
environment that `axi secrets discover` reported clean:

1. **No matcher recognised it.** `openai-key` is ``sk-[A-Za-z0-9]{32,}``,
   which cannot cross a hyphen, so `sk-ant-api03-…` matched nothing — and
   neither did a modern `sk-proj-…`. The platform's own LLM credential was
   invisible to the platform's own credential scanner.
2. **`ProcessEnvProbe` is Linux-only.** It walks ``/proc/<pid>/environ`` and
   returned immediately on macOS — the platform the people being onboarded
   actually develop on. The one probe that would have seen a credential held
   in a live environment was a no-op exactly where it was needed.
3. **Nothing read a shell startup file.** `EnvFileProbe` globs ``*.env``
   under a config root; an export in ``~/.zshrc`` is in none of those.
"""

from __future__ import annotations

from axiom.extensions.builtins.secrets.discovery.matchers import match_text
from axiom.extensions.builtins.secrets.discovery.probes import (
    ProcessEnvProbe,
    ShellRcProbe,
)

# Synthetic, structurally valid, deliberately not anybody's key.
ANTHROPIC = "sk-ant-api03-" + "A" * 80
OPENAI_PROJECT = "sk-proj-" + "B" * 60
OPENAI_FLAT = "sk-" + "C" * 40


class TestHyphenSegmentedVendorKeys:
    def test_an_anthropic_key_is_recognised(self):
        assert "anthropic-key" in [m for m, _ in match_text(ANTHROPIC)]

    def test_a_modern_openai_project_key_is_recognised(self):
        assert "openai-project-key" in [m for m, _ in match_text(OPENAI_PROJECT)]

    def test_the_original_flat_key_still_matches(self):
        """Widening must not drop what already worked."""
        assert "openai-key" in [m for m, _ in match_text(OPENAI_FLAT)]

    def test_a_short_lookalike_is_not_a_key(self):
        assert match_text("sk-ant-short") == []


class TestTheProcessProbeWorksOffLinux:
    def test_without_proc_it_reports_this_process(self, monkeypatch):
        monkeypatch.setenv("SOME_VENDOR_TOKEN", ANTHROPIC)
        hits = list(ProcessEnvProbe(proc_root="/nonexistent").scan(__import__("pathlib").Path("/")))
        assert any("SOME_VENDOR_TOKEN" in h.locator for h in hits)

    def test_it_says_where_the_value_is_held(self, monkeypatch):
        """A locator naming a pid nobody can inspect is not actionable."""
        monkeypatch.setenv("SOME_VENDOR_TOKEN", ANTHROPIC)
        (hit,) = [
            h
            for h in ProcessEnvProbe(proc_root="/nonexistent").scan(
                __import__("pathlib").Path("/")
            )
            if "SOME_VENDOR_TOKEN" in h.locator
        ]
        assert "inherited" in hit.detail

    def test_with_proc_it_still_walks_pids(self, tmp_path, monkeypatch):
        """The fleet view is the better answer where it is available."""
        proc = tmp_path / "proc" / "42"
        proc.mkdir(parents=True)
        (proc / "environ").write_bytes(f"TOKEN={ANTHROPIC}\0".encode())
        (proc / "comm").write_text("some-service\n")
        hits = list(ProcessEnvProbe(proc_root=tmp_path / "proc").scan(tmp_path))
        assert any("pid 42" in h.locator and "some-service" in h.locator for h in hits)


class TestTheShellStartupProbe:
    def test_an_exported_key_is_found(self, tmp_path):
        (tmp_path / ".zshrc").write_text(f'export VENDOR_TOKEN="{ANTHROPIC}"\n')
        (hit,) = list(ShellRcProbe().scan(tmp_path))
        assert hit.locator.endswith(":VENDOR_TOKEN")

    def test_a_commented_example_is_not_a_leak(self, tmp_path):
        (tmp_path / ".zshrc").write_text(f'# export VENDOR_TOKEN="{ANTHROPIC}"\n')
        assert list(ShellRcProbe().scan(tmp_path)) == []

    def test_reading_the_value_from_a_keychain_is_not_a_finding(self, tmp_path):
        """The good pattern must not be reported as the bad one. This line
        holds no credential — it fetches one at shell start."""
        (tmp_path / ".zshrc").write_text(
            'export VENDOR_TOKEN=$(security find-generic-password -s VENDOR_TOKEN -w)\n'
        )
        assert list(ShellRcProbe().scan(tmp_path)) == []

    def test_a_file_that_is_not_there_is_not_an_error(self, tmp_path):
        assert list(ShellRcProbe().scan(tmp_path)) == []


class TestAnUnverifiableSweepIsNotACleanOne:
    """`managed` is decided against an index of known fingerprints. When that
    index is empty every finding lands in `unknown`, so `unmanaged` is zero
    and the sweep reported clean over a host full of loose material.

    That is a check that could not fail. KEEP has run this hourly: the
    heartbeat passes no fingerprints, and the foreign store records none to
    pass, so no run of it could ever have escalated anything.
    """

    def _run(self, tmp_path, monkeypatch, **params):
        """A credential on DISK, not in the environment.

        An earlier version of this set an env var and relied on the
        process-env probe seeing it. That passes on macOS, where the probe
        falls back to reading `os.environ` live, and fails on Linux, where
        it walks `/proc/<pid>/environ` — a snapshot taken at exec, so a
        monkeypatched variable is not in it. The subject here is the
        unverifiable verdict, not the probe, so the fixture uses the one
        source that reads identically on both.
        """
        from axiom.extensions.builtins.secrets.skills import discover

        (tmp_path / "creds.env").write_text(
            f'export SOME_VENDOR_TOKEN="{ANTHROPIC}"\n', encoding="utf-8"
        )
        monkeypatch.setattr(discover, "_LAST_STALE", [])

        class _Ctx:
            state_dir = tmp_path

        return discover.run({"env_root": str(tmp_path), **params}, _Ctx())

    def test_findings_with_no_index_are_not_clean(self, tmp_path, monkeypatch):
        result = self._run(tmp_path, monkeypatch)
        assert result.value["total"] >= 1
        assert result.value["unverifiable"] is True
        assert result.ok is False

    def test_an_index_that_knows_the_value_is_verifiable(self, tmp_path, monkeypatch):
        from axiom.extensions.builtins.secrets.discovery.model import fingerprint

        result = self._run(
            tmp_path, monkeypatch, known_fingerprints=[fingerprint(ANTHROPIC)]
        )
        assert result.value["unverifiable"] is False
        assert result.value["consumers"] >= 1

    def test_an_index_that_does_not_know_it_escalates(self, tmp_path, monkeypatch):
        """The answer the sweep exists to give, which it could never reach."""
        result = self._run(tmp_path, monkeypatch, known_fingerprints=["0" * 16])
        assert result.value["unmanaged"] >= 1
        assert result.ok is False
