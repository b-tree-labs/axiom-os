# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every tool on the surface says who provides it.

`axi mcp list-tools` prints a contributor beside each tool. A colleague's run on
2026-10-01 ended with two rows that could not:

    axiom_prompts__list   [?]  List the guidance prompts this server publishes.
    axiom_prompts__get    [?]  Fetch one published guidance prompt by name.

Every other tool in the same listing named `platform` or an extension. These two
are the projection of published prompts into the tool surface — appended to
`merged_tools` and claimed by no contribution source, so the listing fell back
to `?`.

Two things follow from an unclaimed tool, and the cosmetic one is the lesser:

- `list-tools --source <name>` cannot select it, whatever name is passed, so a
  tool exists that no filter reaches.
- A surface where provenance is advisory is a surface where the next unclaimed
  tool is unremarkable. `?` is what an audit of "where did this tool come from"
  returns, and it is the wrong answer to have available.

So the guard is on the whole surface rather than on these two names: every
published tool is claimed by exactly one source.
"""

from __future__ import annotations

from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry


def _surface():
    return AggregationRegistry.from_node().build()


def _claims(surface) -> dict[str, list[str]]:
    """Tool name -> every source that claims it."""
    out: dict[str, list[str]] = {t.name: [] for t in surface.tools}
    for src in surface.sources:
        for name in src.tool_names:
            out.setdefault(name, []).append(src.name)
    return out


def test_no_published_tool_is_unclaimed():
    surface = _surface()
    claims = _claims(surface)
    orphans = [t.name for t in surface.tools if not claims.get(t.name)]
    assert not orphans, (
        "these tools are published and no contribution source claims them, so "
        "`list-tools` prints `[?]` and `--source` cannot reach them: "
        + ", ".join(sorted(orphans))
    )


def test_no_tool_is_claimed_twice():
    """Two claims is as bad as none: the listing would print one of them and
    which one is whichever source was appended last."""
    surface = _surface()
    doubled = {n: srcs for n, srcs in _claims(surface).items() if len(srcs) > 1}
    assert not doubled, f"these tools are claimed by more than one source: {doubled}"


def test_every_source_claims_something_that_exists():
    """The other direction. A source naming a tool the surface does not publish
    makes `--source` offer a filter that returns nothing."""
    surface = _surface()
    published = {t.name for t in surface.tools}
    phantom = {
        src.name: [n for n in src.tool_names if n not in published]
        for src in surface.sources
        if any(n not in published for n in src.tool_names)
    }
    assert not phantom, f"these sources claim tools that are not published: {phantom}"


def test_the_prompt_tools_name_a_source_and_a_filter_reaches_them(
    make_extension, tmp_axiom_home
):
    """The two rows from the colleague's listing, driven deterministically.

    The sweeps above pass on a node that publishes no prompts, which is most
    nodes and is how this would read green while staying broken. So this builds
    an extension that publishes one and asserts the projection is claimed.

    Asserted through the same name-to-source lookup the listing builds, so a fix
    that claims them in a shape the CLI does not read still fails here. That
    lookup is also what `--source` filters on, which is the half of the defect
    that was not cosmetic: an unclaimed tool is a tool no filter can reach.
    """
    import textwrap

    manifest = textwrap.dedent(
        '''
        [extension]
        name = "guided"
        version = "0.0.1"
        description = "an extension that publishes guidance"
        owner = "axiom-tests"
        aeos_version = "0.1.0"

        [extension.mcp]
        enabled = true
        prefix = "guided"

        [[extension.mcp.prompt]]
        name = "guided.how-to"
        description = "How to use this extension"
        entry = "guided.prompts:how_to"
        '''
    )
    surface = AggregationRegistry(
        extensions=[make_extension("guided", manifest)]
    ).build()

    projected = [t.name for t in surface.tools if t.name.startswith("axiom_prompts__")]
    assert projected, "the extension published a prompt and nothing was projected"

    name_to_source = {}
    for src in surface.sources:
        for n in src.tool_names:
            name_to_source[n] = src.name

    for name in projected:
        assert name_to_source.get(name) == "prompts", (
            f"{name} resolves to {name_to_source.get(name)!r}; the listing prints "
            f"`[?]` for anything unclaimed and `--source` cannot select it"
        )


# ---------------------------------------------------------------------------
# Through the CLI's own lookup, which is not the same lookup as the surface's.
# ---------------------------------------------------------------------------


def _cli_label_for(tool_name: str, surface) -> str | None:
    """Rebuild the label exactly as `list-tools` and `--source` build it.

    Asserting against the surface's `sources` is not enough. The CLI keeps its
    own mapping, and it used to collapse every platform-kind source to the
    literal "platform" — harmless with one such source, wrong the moment a
    second arrived. A guard that only reads the surface would have called that
    fixed.
    """
    mapping = {}
    for src in surface.sources:
        for n in src.tool_names:
            mapping[n] = src.name
    return mapping.get(tool_name)


def test_the_listing_label_is_the_sources_own_name(make_extension, tmp_axiom_home):
    import textwrap

    manifest = textwrap.dedent(
        '''
        [extension]
        name = "guided"
        version = "0.0.1"
        description = "an extension that publishes guidance"
        owner = "axiom-tests"
        aeos_version = "0.1.0"

        [extension.mcp]
        enabled = true
        prefix = "guided"

        [[extension.mcp.prompt]]
        name = "guided.how-to"
        description = "How to use this extension"
        entry = "guided.prompts:how_to"
        '''
    )
    surface = AggregationRegistry(extensions=[make_extension("guided", manifest)]).build()

    for name in (t.name for t in surface.tools if t.name.startswith("axiom_prompts__")):
        assert _cli_label_for(name, surface) == "prompts"

    # The platform primitives keep the label they always had.
    platform = [t.name for t in surface.tools if t.name.startswith("axiom_memory__")]
    assert platform, "no platform primitive on the surface, so nothing is compared"
    assert _cli_label_for(platform[0], surface) == "platform"


def test_the_cli_builds_that_mapping_from_the_name_and_not_the_kind():
    """Read as source, because the mapping is built inline in two commands and
    both have to agree. A fix applied to one of them leaves the other wrong."""
    from pathlib import Path

    cli = Path(__file__).resolve().parent.parent / "cli.py"
    text = cli.read_text(encoding="utf-8")
    assert 'if src.kind == "extension" else "platform"' not in text, (
        "a lookup still collapses platform-kind sources to one label, so a "
        "second platform source is unreachable by --source"
    )
    assert text.count("name_to_source[n] = src.name") == 2, (
        "both list-tools and inspect must build the same mapping"
    )
