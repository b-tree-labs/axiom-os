# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What the model is told it can do must come from what it can actually do.

The base system prompt enumerated a fixed capability list — document
management, signal ingestion, read_file/list_files — written before extensions
registered tools of their own. The tool table is built fresh every turn from
every installed extension; the sentence describing it was frozen.

The consequence, observed against a real node: asked for telemetry metric
names, the assistant answered that `telemetry_metrics` "is not available in the
current environment" while that tool was in the table and being offered
natively. Earlier it reached for `list_files` on an invented path — one of the
tools the stale prompt advertises. It was believing the prompt over its own
tool list, which is the correct thing for it to do and the wrong thing for us
to have told it.

This is what stops any extension getting integrated support for free: a site
can register tools all day and the model is still told it works on documents.
So the description is generated from the registry.
"""

from __future__ import annotations

from axiom.extensions.builtins.chat.tool_summary import describe_available_tools


class _Tool:
    def __init__(self, description: str) -> None:
        self.description = description


def test_it_names_the_tools_that_are_registered():
    summary = describe_available_tools(
        {
            "telemetry_metrics": _Tool("List telemetry metrics with coverage."),
            "read_file": _Tool("Read a file."),
        }
    )

    assert "telemetry_metrics" in summary
    assert "read_file" in summary


def test_a_site_extension_appears_without_any_wiring():
    """The generality requirement: registering is the whole integration."""
    summary = describe_available_tools(
        {"vcu_flow_loop_pump_status": _Tool("Pump status for a flow loop.")}
    )

    assert "vcu_flow_loop_pump_status" in summary


def test_it_does_not_claim_capabilities_that_are_not_registered():
    """Negative control, and the actual bug: the old text advertised document
    publishing and file listing to every deployment whether or not they were
    installed."""
    summary = describe_available_tools({"telemetry_metrics": _Tool("Metrics.")})

    lowered = summary.lower()
    assert "list_files" not in lowered
    assert "publish" not in lowered
    assert "document management" not in lowered


def test_no_tools_says_so_rather_than_inventing_a_list():
    summary = describe_available_tools({})

    assert "telemetry" not in summary.lower()
    assert summary.strip(), "an empty string would leave the prompt silent"


def test_it_tells_the_model_not_to_invent_identifiers():
    """The fabrication half. `Shim1_position` does not exist; the model made it
    up rather than calling the tool that lists real names."""
    summary = describe_available_tools({"telemetry_metrics": _Tool("Metrics.")})

    lowered = summary.lower()
    assert "invent" in lowered or "guess" in lowered or "do not" in lowered


def test_the_base_template_no_longer_hardcodes_a_capability_list():
    """The stale text itself, guarded so it cannot come back."""
    from axiom.infra import prompt_registry

    source = "".join(
        getattr(entry, "content", "") or ""
        for entry in getattr(prompt_registry, "_BUILTIN_TEMPLATES", [])
    )

    assert "data platform with document management capabilities" not in source
    assert "Generate and publish documents" not in source


def test_a_late_alphabet_tool_is_still_named():
    """Truncating the list drops whatever sorts last, and extension tools tend
    to sort last — `telemetry_*`, `vcu_*`. The first version of this capped the
    list at 60 names and lost every telemetry tool, which is the exact bug it
    was written to fix, reintroduced one layer up."""
    many = {f"aaa_builtin_{i:02d}": _Tool("A builtin.") for i in range(80)}
    many["telemetry_metrics"] = _Tool("List telemetry metrics.")
    many["vcu_flow_loop_pump_status"] = _Tool("Pump status.")

    summary = describe_available_tools(many)

    assert "telemetry_metrics" in summary
    assert "vcu_flow_loop_pump_status" in summary


def test_a_large_registry_stays_a_sane_size():
    """Names must all appear; descriptions are what gets dropped, not tools."""
    many = {f"tool_{i:03d}": _Tool("x" * 90) for i in range(200)}

    summary = describe_available_tools(many)

    assert "tool_199" in summary
    assert len(summary) < 20_000
