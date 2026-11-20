# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`list-tools` has to be readable in the terminal it is read in.

Every row was one unwrapped line: a name padded to forty columns whatever its
length, a contributor, then the whole description. Several descriptions on this
surface run past 300 characters, deliberately, because they say what the tool is
for.

On an eighty-column console that is a wall. A colleague ran it on 2026-10-01 on
a Windows terminal and the copy he sent back had rows overlapping each other,
with one tool's name gone and its description bleeding into the row above. The
mangling itself is most likely an artifact of copying a wrapped console buffer
rather than something we emitted. The wall is ours.

So the listing wraps to the width it is printed at, the way `mcp status`
already does, and the name column is sized to the names actually present rather
than padded to a constant.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.mcp.cli import main as mcp_main


def _lines(capsys) -> list[str]:
    return [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]


@pytest.mark.parametrize("columns", [80, 100, 120, 200])
def test_no_row_runs_past_the_terminal_width(columns, monkeypatch, capsys):
    """The whole complaint, at four widths.

    Eighty is the one that matters: it is the default on a fresh Windows
    console, which is where this was met.
    """
    monkeypatch.setenv("COLUMNS", str(columns))
    assert mcp_main(["list-tools"]) == 0
    over = [ln for ln in _lines(capsys) if len(ln) > columns]
    assert not over, (
        f"{len(over)} line(s) run past {columns} columns, the longest by "
        f"{max(len(ln) for ln in over) - columns}: {over[0][:120]!r}"
    )


def test_every_tool_still_appears(monkeypatch, capsys):
    """Wrapping must not be achieved by dropping rows.

    A listing that fits because it stopped listing things is the failure this
    guard would otherwise invite.
    """
    from axiom.extensions.builtins.mcp.cli import _load_or_build_surface

    surface = _load_or_build_surface()
    monkeypatch.setenv("COLUMNS", "80")
    assert mcp_main(["list-tools"]) == 0
    out = "\n".join(_lines(capsys))
    missing = [t.name for t in surface.tools if t.name not in out]
    assert not missing, f"these tools stopped being listed: {missing}"


def test_a_description_is_not_silently_cut(monkeypatch, capsys):
    """Wrapped, not truncated. The descriptions on this surface are where the
    answer to "which tool do I call" lives, so losing their tails to fit a
    column is losing the thing somebody came for."""
    from axiom.extensions.builtins.mcp.cli import _load_or_build_surface

    surface = _load_or_build_surface()
    longest = max(
        (t for t in surface.tools if t.description),
        key=lambda t: len(t.description or ""),
        default=None,
    )
    assert longest is not None, "no tool carries a description, so nothing is under test"

    monkeypatch.setenv("COLUMNS", "80")
    assert mcp_main(["list-tools"]) == 0
    out = " ".join(_lines(capsys))
    # The last few words have to survive the wrap. Compared word-wise because
    # wrapping inserts newlines and padding wherever it likes.
    tail = (longest.description or "").split()[-4:]
    for word in tail:
        assert word in out, (
            f"{longest.name}'s description lost {word!r}; it was cut to fit rather "
            f"than wrapped"
        )


def test_the_contributor_is_still_beside_each_tool(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "80")
    assert mcp_main(["list-tools"]) == 0
    out = "\n".join(_lines(capsys))
    assert "platform" in out, "no contributor is shown"


def test_filtering_by_contributor_still_works(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "80")
    assert mcp_main(["list-tools", "--source", "platform"]) == 0
    out = "\n".join(_lines(capsys))
    assert "axiom_memory__" in out


def test_an_unknown_contributor_still_names_the_real_ones(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "80")
    assert mcp_main(["list-tools", "--source", "not-a-contributor"]) == 1
    err = capsys.readouterr().err
    assert "not-a-contributor" in err
    assert "platform" in err


def test_the_measurement_this_guard_relies_on_is_real(monkeypatch, capsys):
    """A negative control: the width has to actually reach the renderer.

    If `COLUMNS` were ignored, every assertion above would be comparing output
    to a width nothing honoured, and the one at eighty would pass by accident on
    a wide terminal.
    """
    monkeypatch.setenv("COLUMNS", "200")
    mcp_main(["list-tools"])
    wide = max(len(ln) for ln in _lines(capsys))

    monkeypatch.setenv("COLUMNS", "80")
    mcp_main(["list-tools"])
    narrow = max(len(ln) for ln in _lines(capsys))

    assert narrow < wide, (
        "the same listing came out the same width at 80 and 200 columns, so the "
        "width is not reaching the renderer and these tests prove nothing"
    )
