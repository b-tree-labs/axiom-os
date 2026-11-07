"""What the owner declares survives, round-trips, and each change says what it did."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.node import declarations as decl


def test_an_empty_state_is_an_empty_set(tmp_path):
    d = decl.load(tmp_path)
    assert d.engines == [] and d.tiers == [] and d.tracked == [] and d.config_dirs == []


def test_declarations_round_trip_through_the_file(tmp_path):
    d, what = decl.declare(
        tmp_path,
        "engine",
        name="qwen-vllm",
        unit="vllm-server.service",
        port=41883,
        weights="/home/llm/vllm/.hf",
        owner="llm-account",
        engine_kind="vllm",
    )
    assert what == "engine qwen-vllm declared on port 41883"
    decl.declare(tmp_path, "tier", mount="/home", tier="hot", floor_gb=150)
    decl.declare(tmp_path, "tier", mount="/bulk", tier="bulk")
    _, what = decl.declare(
        tmp_path,
        "tracked",
        live="~/landing-conf/default.conf",
        source="serving/frontdoor/default.conf",
    )
    assert "declared as tracked from serving/frontdoor/default.conf" in what
    again = decl.load(tmp_path)
    assert [e.name for e in again.engines] == ["qwen-vllm"]
    assert again.engines[0].port == 41883 and again.engines[0].owner == "llm-account"
    assert {(t.mount, t.tier, t.floor_gb) for t in again.tiers} == {
        ("/home", "hot", 150),
        ("/bulk", "bulk", 0),
    }
    assert again.tracked[0].source == "serving/frontdoor/default.conf"
    assert decl.path_for(tmp_path).read_text().startswith("# What the node's owner told KEEL")


def test_declaring_the_same_thing_again_replaces_it_and_says_so(tmp_path):
    decl.declare(tmp_path, "engine", name="e", port=1)
    d, what = decl.declare(tmp_path, "engine", name="e", port=2)
    assert what == "engine e updated on port 2"
    assert len(d.engines) == 1 and d.engines[0].port == 2


def test_bad_declarations_are_refused_with_the_reason(tmp_path):
    with pytest.raises(ValueError, match="needs a name"):
        decl.declare(tmp_path, "engine", name="")
    with pytest.raises(ValueError, match="hot, bulk"):
        decl.declare(tmp_path, "tier", mount="/x", tier="warm")
    with pytest.raises(ValueError, match="not a declaration kind"):
        decl.declare(tmp_path, "sail", name="x")


def test_a_damaged_file_reads_as_empty_and_says_which_file(tmp_path):
    p = decl.path_for(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("this is = not [toml")
    d = decl.load(tmp_path)
    assert d.engines == [] and d.config_dirs and d.config_dirs[0].startswith("unreadable: ")
