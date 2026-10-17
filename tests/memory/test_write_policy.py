# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for dual write policies + shared-tier transformation (#39).

Per Collaborative Memory §4: fragments written to a shared tier go
through a transformation hook (redact/anonymize/paraphrase/classify)
first. Private tier gets the original; shared gets the sanitized
variant.
"""

from __future__ import annotations


def _frag(content=None):
    from axiom.memory.fragment import create_fragment

    return create_fragment(
        content=content or {"fact": "Alice said her password is hunter2"},
        cognitive_type="semantic",
        principal_id="alice@example.com",
        agents=set(), resources=set(),
    )


class TestWriteScope:
    def test_private_only_writes_to_private_store(self):
        from axiom.memory.write_policy import WriteScope, write_fragment

        private_store = []
        shared_store = []
        f = _frag()
        write_fragment(
            f, scope=WriteScope.PRIVATE,
            private_store=private_store.append,
            shared_store=shared_store.append,
        )
        assert len(private_store) == 1
        assert shared_store == []

    def test_shared_writes_to_both(self):
        from axiom.memory.write_policy import WriteScope, write_fragment

        private_store = []
        shared_store = []
        f = _frag()
        write_fragment(
            f, scope=WriteScope.SHARED,
            private_store=private_store.append,
            shared_store=shared_store.append,
        )
        assert len(private_store) == 1
        assert len(shared_store) == 1


class TestTransformation:
    def test_transform_applied_to_shared_only(self):
        """Private gets original; shared gets transformed."""
        from axiom.memory.write_policy import WriteScope, write_fragment

        def redactor(fragment):
            import dataclasses

            # Replace content's fact with redacted version
            new_content = {**fragment.content, "fact": "[REDACTED]"}
            return dataclasses.replace(fragment, content=new_content,
                                        signature=None)

        priv = []
        shar = []
        f = _frag()
        write_fragment(
            f, scope=WriteScope.SHARED,
            private_store=priv.append,
            shared_store=shar.append,
            transform=redactor,
        )
        assert priv[0].content["fact"] == "Alice said her password is hunter2"
        assert shar[0].content["fact"] == "[REDACTED]"

    def test_no_transform_passes_fragment_unchanged(self):
        from axiom.memory.write_policy import WriteScope, write_fragment

        priv = []
        shar = []
        f = _frag()
        write_fragment(
            f, scope=WriteScope.SHARED,
            private_store=priv.append,
            shared_store=shar.append,
        )
        # Same fragment instance to both
        assert priv[0].content == shar[0].content


class TestBuiltInTransforms:
    def test_anonymize_principal(self):
        from axiom.memory.write_policy import anonymize_principal

        f = _frag()
        assert f.provenance.principal_id == "alice@example.com"
        t = anonymize_principal(f)
        assert t.provenance.principal_id != "alice@example.com"
        assert t.provenance.principal_id.startswith("anon-")

    def test_redact_pattern(self):
        import re

        from axiom.memory.write_policy import redact_pattern

        f = _frag(content={"fact": "my email is alice@example.com and SSN is 123-45-6789"})
        # Redact email addresses
        email_re = re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b")
        t = redact_pattern(f, pattern=email_re, replacement="[EMAIL]")
        assert "alice@example.com" not in t.content["fact"]
        assert "[EMAIL]" in t.content["fact"]

    def test_redact_multiple_patterns_composed(self):
        """Composition: apply multiple transforms sequentially."""
        import re

        from axiom.memory.write_policy import compose_transforms, redact_pattern

        def email_redact(f):
            return redact_pattern(
                    f, re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b"), "[EMAIL]"
                )
        def ssn_redact(f):
            return redact_pattern(
                    f, re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"
                )
        t_combined = compose_transforms([email_redact, ssn_redact])

        f = _frag(content={"fact": "email alice@example.com SSN 123-45-6789"})
        out = t_combined(f)
        assert "[EMAIL]" in out.content["fact"]
        assert "[SSN]" in out.content["fact"]


class TestPolicyDrivenRouting:
    """Policy coordinate drives the scope selection."""

    def test_policy_allows_shared_writes(self):
        from axiom.memory.policy import PolicyCoord, with_global
        from axiom.memory.write_policy import (
            WriteScope,
            scope_from_policy,
        )

        coord = with_global(PolicyCoord(), {"write": "shared"})
        scope = scope_from_policy(coord, user="u1", agent="a1",
                                   at="2026-04-17T00:00:00Z")
        assert scope == WriteScope.SHARED

    def test_policy_denies_shared_default_private(self):
        from axiom.memory.policy import PolicyCoord, with_global
        from axiom.memory.write_policy import (
            WriteScope,
            scope_from_policy,
        )

        coord = with_global(PolicyCoord(), {"write": "private"})
        scope = scope_from_policy(coord, "u1", "a1", "2026-04-17T00:00:00Z")
        assert scope == WriteScope.PRIVATE

    def test_no_policy_defaults_private(self):
        from axiom.memory.policy import PolicyCoord
        from axiom.memory.write_policy import WriteScope, scope_from_policy

        coord = PolicyCoord()
        assert scope_from_policy(coord, "u1", "a1", "2026-04-17T00:00:00Z") == WriteScope.PRIVATE
