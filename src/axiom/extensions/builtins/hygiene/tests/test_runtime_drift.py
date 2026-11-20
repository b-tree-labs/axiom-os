# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What code is this node actually running, and does it match what it should?

Three drifts, each found live before a check existed:

- #673  the package resolves through an editable install whose checkout sits
        on a feature branch, dirty, or behind the newest release tag, so every
        runtime surface silently runs that branch;
- #703  a service Axiom installed calls a runtime other than the CLI's (a
        second venv, another checkout), so a verb that exists in one is
        "invalid choice" in the other;
- #703  a long-lived supervisor started its children with the environment it
        had at boot, so a key added to the unit's EnvironmentFile never reaches
        the serving copy until a full restart.

Every check is pure over collected facts, so these tests never touch git, a
venv, /proc or systemd.
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.hygiene.node_health import Severity
from axiom.extensions.builtins.hygiene.runtime_drift import (
    EditableRuntime,
    RuntimeIdentity,
    ServingEnv,
    check_editable_runtime,
    check_runtime_parity,
    check_serving_env_drift,
    collect_editable_runtime,
    editable_target_from_direct_url,
    env_file_keys,
)

CHECKOUT = Path("/srv/checkout")


def _at_tag(**kw) -> EditableRuntime:
    base = dict(
        target=CHECKOUT,
        branch=None,
        exact_tag="v1.3.0",
        dirty=False,
        newest_release_tag="v1.3.0",
        contains_newest=True,
    )
    base.update(kw)
    return EditableRuntime(**base)


# ---------------------------------------------------------------- #673


class TestEditableTarget:
    def test_an_editable_install_names_its_checkout(self):
        raw = '{"url": "file:///srv/checkout", "dir_info": {"editable": true}}'
        assert editable_target_from_direct_url(raw) == CHECKOUT

    def test_a_wheel_install_is_not_editable(self):
        raw = '{"url": "https://example.invalid/x.whl", "archive_info": {}}'
        assert editable_target_from_direct_url(raw) is None

    def test_no_record_is_not_editable(self):
        assert editable_target_from_direct_url("") is None
        assert editable_target_from_direct_url("not json") is None


class TestEditableRuntimeCheck:
    def test_no_editable_install_says_nothing(self):
        assert check_editable_runtime(None) == []

    def test_exactly_at_the_newest_release_tag_is_healthy(self):
        assert check_editable_runtime(_at_tag()) == []

    def test_a_feature_branch_is_named(self):
        (f,) = check_editable_runtime(_at_tag(branch="feat/x", exact_tag=None))
        assert f.severity is Severity.WARNING
        assert f.check == "editable_runtime"
        assert "feat/x" in f.message
        assert "not at a release tag" in f.message
        assert str(CHECKOUT) in f.message

    def test_dirty_is_named(self):
        (f,) = check_editable_runtime(_at_tag(dirty=True))
        assert "uncommitted changes" in f.message

    def test_behind_the_newest_release_is_named(self):
        facts = _at_tag(exact_tag="v0.70.36", newest_release_tag="v1.3.0", contains_newest=False)
        (f,) = check_editable_runtime(facts)
        assert "behind the newest release tag v1.3.0" in f.message

    def test_every_condition_is_named_in_one_finding(self):
        facts = _at_tag(
            branch="wip", exact_tag=None, dirty=True, newest_release_tag="v1.3.0", contains_newest=False
        )
        (f,) = check_editable_runtime(facts)
        for part in ("wip", "uncommitted changes", "behind the newest release tag v1.3.0"):
            assert part in f.message

    def test_a_formal_install_path_node_reports_any_editable_install_as_policy(self):
        findings = check_editable_runtime(_at_tag(), formal_install_path=True)
        assert [(f.check, f.severity) for f in findings] == [
            ("editable_install_policy", Severity.INFO)
        ]


class TestCollectEditableRuntime:
    def _git(self, answers: dict[tuple[str, ...], tuple[int, str]]):
        def run(cmd, timeout=10):
            key = tuple(cmd[3:])  # drop "git -C <target>"
            return answers.get(key, (1, ""))

        return run

    def test_detached_at_the_newest_tag(self):
        run = self._git({
            ("symbolic-ref", "-q", "--short", "HEAD"): (1, ""),
            ("describe", "--tags", "--exact-match", "HEAD"): (0, "v1.3.0"),
            ("status", "--porcelain", "--untracked-files=no"): (0, ""),
            ("tag", "--list", "v*", "--sort=-v:refname"): (0, "v1.3.0\nv0.70.36"),
            ("merge-base", "--is-ancestor", "v1.3.0", "HEAD"): (0, ""),
        })
        facts = collect_editable_runtime(CHECKOUT, run=run)
        assert facts == _at_tag()

    def test_a_dirty_branch_behind_the_newest_tag(self):
        run = self._git({
            ("symbolic-ref", "-q", "--short", "HEAD"): (0, "feat/x"),
            ("describe", "--tags", "--exact-match", "HEAD"): (128, ""),
            ("status", "--porcelain", "--untracked-files=no"): (0, " M src/a.py"),
            ("tag", "--list", "v*", "--sort=-v:refname"): (0, "v1.3.0"),
            ("merge-base", "--is-ancestor", "v1.3.0", "HEAD"): (1, ""),
        })
        facts = collect_editable_runtime(CHECKOUT, run=run)
        assert facts.branch == "feat/x"
        assert facts.exact_tag is None
        assert facts.dirty is True
        assert facts.contains_newest is False


# ---------------------------------------------------------------- #703 parity


class TestRuntimeParity:
    CLI = RuntimeIdentity(interpreter="/venv/bin/python", version="1.3.0", package_root="/srv/main")

    def test_a_service_on_the_cli_runtime_is_quiet(self):
        records = {"com.example.capture": self.CLI}
        assert check_runtime_parity(records, self.CLI) == []

    def test_a_service_on_another_version_and_checkout_is_named(self):
        svc = RuntimeIdentity(
            interpreter="/svc/bin/python", version="0.70.1", package_root="/srv/feature"
        )
        (f,) = check_runtime_parity({"com.example.capture": svc}, self.CLI)
        assert f.check == "runtime_parity"
        assert f.severity is Severity.WARNING
        assert "com.example.capture" in f.message
        assert "0.70.1" in f.message and "1.3.0" in f.message
        assert "/srv/feature" in f.message

    def test_a_service_whose_runtime_cannot_be_probed_is_unknown_not_drift(self):
        svc = RuntimeIdentity(interpreter="/gone/bin/python", error="No such file")
        (f,) = check_runtime_parity({"com.example.capture": svc}, self.CLI)
        assert f.check == "runtime_parity"
        assert "could not be measured" in f.message
        assert "No such file" in f.message


# ---------------------------------------------------------------- #703 env keys


class TestServingEnvDrift:
    def test_env_file_keys_reads_names_only(self):
        text = "# c\nA=1\nexport B='two'\n\nC = 3\nnot a line\n"
        assert env_file_keys(text) == {"A", "B", "C"}

    def test_a_child_with_every_file_key_is_quiet(self):
        s = ServingEnv(
            unit="site.service", env_file="/etc/site.env",
            file_keys={"A", "B"}, child_pid=42, child_keys={"A", "B", "PATH"},
        )
        assert check_serving_env_drift([s]) == []

    def test_a_key_the_serving_child_never_received_is_named_without_its_value(self):
        s = ServingEnv(
            unit="site.service", env_file="/etc/site.env",
            file_keys={"A", "NEW_SECRET_KEY"}, child_pid=42, child_keys={"A"},
        )
        (f,) = check_serving_env_drift([s])
        assert f.check == "serving_env_drift"
        assert f.severity is Severity.WARNING
        assert "NEW_SECRET_KEY" in f.message
        assert "systemctl restart site.service" in f.message
        assert "=" not in f.current_value  # names, never values

    def test_an_unreadable_side_says_nothing(self):
        s = ServingEnv(
            unit="site.service", env_file="/etc/site.env",
            file_keys=None, child_pid=42, child_keys={"A"},
        )
        assert check_serving_env_drift([s]) == []


# ---------------------------------------------------------------- heartbeat


def test_audit_node_surfaces_the_runtime_drift_findings():
    """The checks ride the existing heartbeat (`hygiene stat health`)."""
    from axiom.extensions.builtins.hygiene.node_health import audit_node
    from axiom.extensions.builtins.hygiene.tests.test_node_health import (
        _inert_corpus,
        _inert_store,
    )

    report = audit_node(
        run=lambda cmd, timeout=10: (1, ""),
        version_checker=lambda *a, **k: None,
        reclaimable_branches=lambda repo: [],
        corpus_facts=_inert_corpus(),
        store_facts=_inert_store(),
        attestation_facts=[],
        runtime_facts={
            "editable": _at_tag(branch="feat/x", exact_tag=None),
            "formal_install_path": False,
            "records": {},
            "current": None,
            "serving_env": [],
        },
    )
    assert "editable_runtime" in {f.check for f in report.findings}
