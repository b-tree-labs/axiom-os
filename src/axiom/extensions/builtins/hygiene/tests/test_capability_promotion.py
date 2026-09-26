# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The introspective check that stops capability drift, and teaches.

A ratchet stops the count growing. It teaches nobody, so the next extension
author writes `registry.register(name, fn)` — the form every example uses — and
ships a capability that nothing can find. Half the platform got there that way:
58 of 117 capabilities carry no SkillSpec, so they are invisible to MCP, to the
agent tool loop, to SKILL.md and to the discovery block.

This is the crutch. It reconciles what is registered against what is DECLARED,
writes the missing declaration for you, and says what promoting each one buys —
per capability, not as a paragraph of doctrine nobody reads.

It lives in hygiene because hygiene is the drift surface AND the one daemon the
operator has consented to run. A check on a disabled daemon is the failure this
whole episode was about.
"""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.hygiene.skills.capabilities import (
    promotion_report,
    proposed_spec_source,
)
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec


def _read(params, ctx):
    """Report the current widget count."""
    return SkillResult(ok=True)


def _write(params, ctx):
    """Delete a widget and everything it owns."""
    return SkillResult(ok=True)


def _undocumented(params, ctx):
    return SkillResult(ok=True)


@pytest.fixture
def registry():
    reg = SkillRegistry()
    reg.register("demo.count", _read, mutating=False)
    reg.register("demo.destroy", _write, mutating=True)
    reg.register("demo.mystery", _undocumented, mutating=False)
    reg.register_skill(
        SkillSpec(name="demo.promoted", fn=_read, description="Already declared.",
                  side_effects=False, surfaces=("cli", "mcp")),
    )
    return reg


@pytest.fixture
def ctx(registry, tmp_path):
    return SkillContext(registry=registry, state_dir=tmp_path,
                        logger=logging.getLogger("t"))


def test_it_separates_declared_from_merely_registered(registry, ctx):
    report = promotion_report(registry)
    assert set(report["unpromoted"]) == {"demo.count", "demo.destroy", "demo.mystery"}
    assert report["promoted"] == ["demo.promoted"]


def test_a_read_verb_is_proposed_for_every_surface(registry, ctx):
    """A read-only capability is the easy win: it can go everywhere at once."""
    proposal = promotion_report(registry)["proposals"]["demo.count"]
    assert proposal["side_effects"] is False
    assert set(proposal["surfaces"]) == {"cli", "mcp", "agent_tool"}


def test_a_mutating_verb_is_not_proposed_for_mcp(registry, ctx):
    """A write reaching a protocol surface is a decision, not a default. The
    proposal keeps it CLI-only and says why, so accepting the suggestion
    verbatim can never widen an effect nobody chose."""
    proposal = promotion_report(registry)["proposals"]["demo.destroy"]
    assert proposal["side_effects"] is True
    assert "mcp" not in proposal["surfaces"]
    assert "effect" in proposal["why_not_mcp"].lower()


def test_a_description_is_taken_from_the_docstring_not_invented(registry, ctx):
    proposal = promotion_report(registry)["proposals"]["demo.count"]
    assert proposal["description"] == "Report the current widget count."


def test_an_undocumented_capability_is_flagged_not_given_a_fake_description():
    """Inventing "Run demo.mystery" would produce a line that teaches nothing
    and looks authored, so nobody would ever fix it. The gap stays visible."""
    reg = SkillRegistry()
    reg.register("demo.mystery", _undocumented, mutating=False)
    proposal = promotion_report(reg)["proposals"]["demo.mystery"]
    assert proposal["description"] == ""
    assert proposal["blocked_on"], "an undocumented capability must say what it needs"


def test_the_proposal_is_paste_ready_source(registry, ctx):
    """A finding that leaves the author to write the fix gets deferred. The
    point of a crutch is that it carries you."""
    source = proposed_spec_source(promotion_report(registry)["proposals"]["demo.count"])
    assert "SkillSpec(" in source
    assert 'name="demo.count"' in source
    assert "surfaces=(" in source
    compile(source, "<proposal>", "eval")


def test_it_explains_what_promotion_BUYS(registry, ctx):
    """The educational requirement, and it must be concrete. "Conformance" is
    not a reason anybody acts on; "callable from Claude Code as
    axiom_demo__count" is."""
    proposal = promotion_report(registry)["proposals"]["demo.count"]
    gains = " ".join(proposal["gains"]).lower()
    assert "axiom_demo__count" in gains
    assert "discovery" in gains
    for phrase in ("authority", "telemetry"):
        assert phrase in gains


def test_a_mutating_verb_is_told_what_it_gains_WITHOUT_mcp(registry, ctx):
    """A write verb still gains from a spec — it just gains different things.
    Telling it only what it cannot have would read as a punishment."""
    proposal = promotion_report(registry)["proposals"]["demo.destroy"]
    assert proposal["gains"], "a write capability was offered no reason to declare"


def test_nothing_is_applied_automatically(registry, ctx):
    """Editing somebody's extension because a heuristic inferred a description
    is exactly the drift this is meant to stop, arriving from the other side."""
    report = promotion_report(registry)
    assert report["applied"] == []
    assert report["requires_human"] is True


def test_an_all_declared_registry_reports_clean(ctx):
    reg = SkillRegistry()
    reg.register_skill(SkillSpec(name="a.b", fn=_read, description="x",
                                 side_effects=False, surfaces=("cli",)))
    report = promotion_report(reg)
    assert report["unpromoted"] == []
    assert report["clean"] is True


# --- the conservative default hides the easy wins -----------------------------


def test_a_read_shaped_verb_registered_as_mutating_gets_a_hint():
    """`registry.register(name, fn)` defaults to mutating=True, so a status or
    list verb that never declared otherwise is proposed CLI-only. That is the
    safe default and it is usually WRONG, which buries exactly the capabilities
    that could reach every surface today.

    The hint says so and names the one-word change. It must NOT widen the
    proposal itself — guessing "this looks read-only" and handing out an MCP
    surface is how a write ends up on a protocol.
    """
    reg = SkillRegistry()
    reg.register("demo.status", _read)  # no mutating= → defaults to True

    proposal = promotion_report(reg)["proposals"]["demo.status"]
    assert proposal["side_effects"] is True
    assert "mcp" not in proposal["surfaces"], "a hint must not widen the proposal"
    assert proposal["hint"], "a read-shaped verb got no hint"
    assert "mutating=False" in proposal["hint"]
    assert "axiom_demo__status" in proposal["hint"]


def test_a_write_shaped_verb_gets_no_such_hint():
    """Negative control. If every verb got the hint it would be noise, and the
    one place it matters would be skipped with the rest."""
    reg = SkillRegistry()
    reg.register("demo.destroy", _write)
    assert not promotion_report(reg)["proposals"]["demo.destroy"]["hint"]


def test_a_verb_that_declared_itself_mutating_is_taken_at_its_word():
    """`purge` is read-shaped by no stretch, but the real rule is that an
    explicit declaration wins over the name. Nothing here second-guesses an
    author who said what their verb does."""
    reg = SkillRegistry()
    reg.register("demo.list", _write, mutating=True)
    proposal = promotion_report(reg)["proposals"]["demo.list"]
    assert proposal["side_effects"] is True


# --- the routine check: it has to actually fire -------------------------------


def test_capability_drift_is_a_node_health_finding():
    """Hooked into the heartbeat, not left as a verb somebody remembers to run.

    `vault steward` proved the other way round: a rotation pass nobody invoked
    stayed broken for weeks. hygiene is the one daemon this operator has
    consented to, and `hygiene stat health` is its heartbeat command, so a
    finding here is a check that genuinely runs.
    """
    from axiom.extensions.builtins.hygiene.node_health import (
        Severity,
        check_capability_declarations,
    )

    reg = SkillRegistry()
    reg.register("demo.count", _read, mutating=False)
    reg.register_skill(SkillSpec(name="demo.ok", fn=_read, description="x",
                                 side_effects=False, surfaces=("cli",)))

    finding = check_capability_declarations(registry=reg)
    assert finding is not None
    assert finding.severity is Severity.WARNING
    assert "demo.count" in finding.message or "1" in finding.current_value
    assert "axi hygiene stat capabilities" in finding.message


def test_a_fully_declared_install_produces_no_finding():
    """Negative control: this must be able to come back clean, or it is noise
    that trains people to skip the whole health report."""
    from axiom.extensions.builtins.hygiene.node_health import (
        check_capability_declarations,
    )

    reg = SkillRegistry()
    reg.register_skill(SkillSpec(name="demo.ok", fn=_read, description="x",
                                 side_effects=False, surfaces=("cli",)))
    assert check_capability_declarations(registry=reg) is None


def test_the_check_never_raises_into_the_heartbeat():
    """An introspection bug must not take down the node health audit — the one
    report that does run."""
    from axiom.extensions.builtins.hygiene.node_health import (
        check_capability_declarations,
    )

    class _Broken:
        def __getattr__(self, name):
            raise RuntimeError("boom")

    assert check_capability_declarations(registry=_Broken()) is None


def test_the_proposal_references_the_real_function_name():
    """Found by running the generator against the real registry: it derived
    `fn=` from the VERB, so `schedule.list` proposed `fn=list` — the Python
    builtin. Pasting that compiles and registers the wrong callable, which is
    worse than a proposal that does not compile, because it looks right.
    """
    def _oddly_named_handler(params, ctx):
        """Do a thing."""
        return SkillResult(ok=True)

    reg = SkillRegistry()
    reg.register("demo.list", _oddly_named_handler, mutating=False)

    source = proposed_spec_source(promotion_report(reg)["proposals"]["demo.list"])
    assert "fn=_oddly_named_handler" in source
    assert "fn=list," not in source


# --- corpus storage on the heartbeat ------------------------------------------


def test_corpus_storage_is_a_node_health_finding():
    """Size was found by an outage. This is the check that finds it first."""
    from axiom.extensions.builtins.hygiene.node_health import (
        Severity,
        check_corpus_storage,
    )
    from axiom.rag.storage_stat import IndexFact, analyze

    report = analyze(
        rows=5_304_997, heap_bytes=8_262_057_984, toast_bytes=26_843_545_600,
        indexes=[
            IndexFact("i_vec", 38_654_705_664, "USING ivfflat (embedding)", 2761),
            IndexFact("a", 1_876_951_040, "CREATE INDEX a ON t USING gin (expr)", 112),
            IndexFact("b", 1_875_902_464, "CREATE INDEX b ON t USING gin (expr)", 1904),
        ],
    )
    finding = check_corpus_storage(report=report)
    assert finding is not None
    assert finding.severity is Severity.WARNING
    assert "duplicated index" in finding.message
    assert "axi rag status" in finding.message


def test_a_healthy_corpus_produces_no_finding():
    """Negative control: it must be able to come back clean, or it is noise
    that trains people to skip the whole report."""
    from axiom.extensions.builtins.hygiene.node_health import check_corpus_storage
    from axiom.rag.storage_stat import IndexFact, analyze

    report = analyze(rows=1000, heap_bytes=1_000_000, toast_bytes=1_000_000,
                     indexes=[IndexFact("i", 500_000, "USING btree (id)", 42)])
    assert check_corpus_storage(report=report) is None


def test_an_empty_corpus_is_not_a_finding():
    from axiom.extensions.builtins.hygiene.node_health import check_corpus_storage
    from axiom.rag.storage_stat import analyze

    assert check_corpus_storage(report=analyze(rows=0, heap_bytes=0, toast_bytes=0,
                                               indexes=[])) is None
