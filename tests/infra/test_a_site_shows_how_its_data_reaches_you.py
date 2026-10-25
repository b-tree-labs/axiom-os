# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A person looking at a site's page can see how its data gets there.

A partner who installs a collector and then signs in to the hosting site's web
app sees their data, and no connection between that page and the machine in
their lab. The topology is that connection: each hop from the collector to the
page they are reading, with when it was last heard from, and a hop that is not
built yet shown as planned, never as broken.
"""

from __future__ import annotations

from axiom.infra import topology

NOW = 1_000_000.0


def _node(name="daq-pc-1", age=30.0, **beat):
    return {
        "node": name,
        "received_at": NOW - age,
        "latest": {
            "node": name,
            "collector": "running",
            "last_reading_at": "2026-10-07T02:59:58Z",
            "readings_today": 1200,
            "pending": 0,
            "route": [{"hop": "ingest edge", "url": "https://edge.example.org", "state": "ok"}],
            **beat,
        },
    }


def test_a_running_collector_is_drawn_from_its_lab_to_the_page():
    view = topology.build("site-a", [_node()], here={"node": "host-1", "url": "https://host.example.org"}, now=NOW)
    names = [h["name"] for h in view["hops"]]
    assert names == ["daq-pc-1", "ingest edge", "data platform", "web app"]
    assert view["hops"][0]["state"] == "online"
    assert view["hops"][-1]["you_are_here"] is True
    assert "daq-pc-1" in view["caption"] and "host-1" in view["caption"]


def test_a_hop_that_is_not_built_is_planned_not_broken():
    view = topology.build(
        "site-a", [], here={"node": "host-1"},
        planned=[{"name": "ingest edge", "where": "hosted at a research computing center"}],
        now=NOW,
    )
    states = {h["name"]: h["state"] for h in view["hops"]}
    assert states["collector"] == "planned"
    assert states["ingest edge"] == "planned"
    assert "not installed yet" in view["caption"]
    assert "broken" not in str(view).lower()


def test_a_silent_collector_is_offline_with_how_long():
    view = topology.build("site-a", [_node(age=7200)], here={"node": "host-1"}, now=NOW)
    first = view["hops"][0]
    assert first["state"] == "offline"
    assert first["last_contact_s"] == 7200


def test_a_collector_with_an_error_says_so_on_its_hop():
    view = topology.build("site-a", [_node(last_error="folder not found", collector="stopped")],
                          here={"node": "host-1"}, now=NOW)
    first = view["hops"][0]
    assert first["error"] == "folder not found"
    assert first["detail"]["collector"] == "stopped"
