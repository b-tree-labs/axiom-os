# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.agents.pipeline.sandbox — declared reach + enforcer vocabulary.

Per analysis §10.2: the user sees declared *reach* ("what dirs / what net");
the sandbox is the enforcer. This module supplies the reach + sandbox-class
contract; container/VM enforcement implementations land in Stage 2.
"""

from __future__ import annotations

from axiom.agents.pipeline.plan import StepReach
from axiom.agents.pipeline.sandbox import (
    NoSandboxEnforcer,
    ReachViolation,
    SandboxClass,
    SandboxEnforcer,
    SandboxSpec,
    classify_reach,
    summarize_reach,
)


class TestSandboxClass:
    def test_class_values(self):
        assert SandboxClass.NONE.value == "none"
        assert SandboxClass.READ_ONLY.value == "read_only"
        assert SandboxClass.EPHEMERAL_CONTAINER.value == "ephemeral_container"
        assert SandboxClass.VM.value == "vm"


class TestClassifyReach:
    def test_empty_reach_is_none(self):
        reach = StepReach()
        assert classify_reach(reach) == SandboxClass.NONE

    def test_read_only_reach_is_read_only(self):
        reach = StepReach(reads=("/repo/**",))
        assert classify_reach(reach) == SandboxClass.READ_ONLY

    def test_writes_promote_to_ephemeral_container(self):
        reach = StepReach(reads=("/repo/**",), writes=("/repo/src/**",))
        assert classify_reach(reach) == SandboxClass.EPHEMERAL_CONTAINER

    def test_network_promotes_to_ephemeral_container(self):
        reach = StepReach(reads=(), writes=(), network=("api.openai.com",))
        assert classify_reach(reach) == SandboxClass.EPHEMERAL_CONTAINER

    def test_writes_plus_network_stays_ephemeral(self):
        reach = StepReach(
            reads=("/repo/**",),
            writes=("/repo/src/**",),
            network=("api.openai.com",),
        )
        assert classify_reach(reach) == SandboxClass.EPHEMERAL_CONTAINER

    def test_root_writes_promote_to_vm(self):
        # Writing to / or system paths is escalated to VM-class.
        reach = StepReach(writes=("/etc/**",))
        assert classify_reach(reach) == SandboxClass.VM

    def test_unrestricted_network_promotes_to_vm(self):
        # "*" or empty-allowlist with "any" indicator → VM-class.
        reach = StepReach(network=("*",))
        assert classify_reach(reach) == SandboxClass.VM


class TestSandboxSpec:
    def test_spec_from_reach_assigns_class(self):
        reach = StepReach(reads=("/repo/**",), writes=("/repo/src/**",))
        spec = SandboxSpec.from_reach(reach)
        assert spec.sandbox_class == SandboxClass.EPHEMERAL_CONTAINER
        assert spec.reach == reach

    def test_spec_can_override_class(self):
        reach = StepReach(reads=("/repo/**",))
        spec = SandboxSpec.from_reach(reach, override_class=SandboxClass.VM)
        assert spec.sandbox_class == SandboxClass.VM


class TestSummarizeReach:
    def test_summary_empty(self):
        reach = StepReach()
        assert "no reach" in summarize_reach(reach).lower()

    def test_summary_includes_counts(self):
        reach = StepReach(
            reads=("/a/**", "/b/**"),
            writes=("/c/**",),
            network=("api.x.com",),
        )
        s = summarize_reach(reach)
        # User-facing summary: "reads N paths, writes M paths, network K hosts"
        assert "2" in s and "1" in s

    def test_summary_user_facing_no_jargon(self):
        reach = StepReach(reads=("/repo/**",))
        s = summarize_reach(reach)
        # Should not say "ephemeral_container" or "sandbox"
        assert "sandbox" not in s.lower()
        assert "container" not in s.lower()


class TestNoSandboxEnforcer:
    def test_enforce_passes_through(self):
        enforcer = NoSandboxEnforcer()
        reach = StepReach(reads=("/a/**",))
        # Should not raise; returns a no-op context manager.
        with enforcer.enforce(reach):
            pass

    def test_check_violation_returns_none_when_within_reach(self):
        enforcer = NoSandboxEnforcer()
        reach = StepReach(reads=("/repo/**",))
        violation = enforcer.check_violation(reach, "/repo/src/main.py", "read")
        assert violation is None

    def test_check_violation_detects_out_of_reach_read(self):
        enforcer = NoSandboxEnforcer()
        reach = StepReach(reads=("/repo/**",))
        violation = enforcer.check_violation(reach, "/etc/passwd", "read")
        assert violation is not None
        assert isinstance(violation, ReachViolation)
        assert violation.path == "/etc/passwd"
        assert violation.operation == "read"

    def test_check_violation_detects_out_of_reach_write(self):
        enforcer = NoSandboxEnforcer()
        reach = StepReach(reads=("/repo/**",), writes=("/repo/src/**",))
        violation = enforcer.check_violation(reach, "/repo/tests/x.py", "write")
        assert violation is not None
        assert violation.operation == "write"

    def test_check_violation_detects_out_of_reach_network(self):
        enforcer = NoSandboxEnforcer()
        reach = StepReach(network=("api.openai.com",))
        violation = enforcer.check_violation(
            reach, "evil.com", "network"
        )
        assert violation is not None

    def test_glob_matching(self):
        enforcer = NoSandboxEnforcer()
        # Test that ** glob matches nested paths.
        reach = StepReach(reads=("/repo/**",))
        assert enforcer.check_violation(reach, "/repo/a/b/c.py", "read") is None
        assert enforcer.check_violation(reach, "/other/x.py", "read") is not None


class TestSandboxEnforcerProtocol:
    def test_no_sandbox_conforms(self):
        enforcer = NoSandboxEnforcer()
        assert isinstance(enforcer, SandboxEnforcer)
