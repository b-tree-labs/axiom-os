"""A node with no connectors says so, instead of reporting an empty catalog.

On a colleague's laptop the bronze catalog walked nothing, because nothing is
registered there, and answered ``objects: []`` exactly as a configured, empty
tree would. Their agent read that as "the site holds no data" and said so,
twice. Three absences collapsed into one answer: nothing registered, registered
but empty, registered with every disposition refused. The first is a fact about
this machine, not about anyone's data, and the answer has to say which.
"""

from __future__ import annotations

from types import SimpleNamespace

from ..resolvers import BronzeResolver


def test_no_connectors_is_reported_as_nothing_registered(tmp_path):
    out = BronzeResolver().catalog({}, SimpleNamespace(state_dir=tmp_path))
    assert out["data"]["objects"] == []
    assert out["data"]["connectors_registered"] == 0
    assert out["provenance"]["absence"] == "no_connectors_on_this_node"
    note = out["provenance"]["note"]
    assert "not evidence" in note and "served" in note


def test_a_registered_connector_is_not_reported_as_absent(tmp_path, monkeypatch):
    from .. import resolvers

    root = tmp_path / "bronze"
    (root / "c1").mkdir(parents=True)
    monkeypatch.setattr(resolvers, "_connector_roots", lambda params, ctx: [("c1", root)])
    out = BronzeResolver().catalog({}, SimpleNamespace(state_dir=tmp_path))
    assert out["data"]["connectors_registered"] == 1
    assert "absence" not in out["provenance"]
