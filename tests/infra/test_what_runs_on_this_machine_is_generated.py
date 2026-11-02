# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The "What runs on this machine" disclosure is generated, and cannot drift.

A partner's information security office reads one page: which processes run,
what listens, where data goes and what it is. Written by hand it goes stale the
first time a deployment changes. It is generated from the deployment
definition itself, and a check fails when a definition adds a listening port
or an outbound destination the published page does not list.
"""

from __future__ import annotations

import pytest

from axiom.infra import disclosure

COMPOSE = {
    "x-axiom-node": {"role": "collector", "functions": ["acquire", "transmit"], "agents": "none"},
    "x-axiom-egress": [
        {"host": "intake.example.org", "port": 443, "purpose": "the data intake", "data": "sensor readings with units and timestamps"},
        {"host": "pypi.org", "port": 443, "purpose": "packages and updates", "data": "nothing about the site"},
    ],
    "services": {
        "collector": {
            "image": "collector:1.0",
            "restart": "unless-stopped",
            "network_mode": "host",
            "x-axiom-listen": [{"address": "127.0.0.1", "port": 8765, "purpose": "the read-only monitor page"}],
            "environment": {"INTAKE_URL": "https://intake.example.org/ingest"},
        },
        "medallion-db": {
            "image": "timescale/timescaledb:2",
            "restart": "unless-stopped",
            "ports": ["127.0.0.1:5432:5432"],
        },
    },
}


def test_the_page_lists_processes_listeners_destinations_and_policy():
    page = disclosure.render(COMPOSE)
    for expected in (
        "collector", "medallion-db",
        "127.0.0.1:8765", "127.0.0.1:5432",
        "intake.example.org:443", "sensor readings with units and timestamps",
        "pypi.org:443",
        'agents = "none"', "no agent runs", "calls no language model",
        "Nothing listens for connections from other machines",
    ):
        assert expected in page, expected


def test_a_published_page_matches_its_definition():
    assert disclosure.drift(COMPOSE, disclosure.render(COMPOSE)) == []


def test_a_new_port_is_caught():
    changed = {**COMPOSE, "services": {**COMPOSE["services"], "extra": {"image": "x", "ports": ["0.0.0.0:9000:9000"]}}}
    problems = disclosure.drift(changed, disclosure.render(COMPOSE))
    assert any("9000" in p for p in problems)


def test_a_port_open_to_other_machines_is_called_out_as_inbound():
    exposed = {**COMPOSE, "services": {"web": {"image": "x", "ports": ["8080:8080"]}}}
    page = disclosure.render(exposed)
    assert "accepts connections from other machines" in page and "8080" in page


def test_an_undeclared_destination_is_caught():
    changed = {**COMPOSE, "services": {**COMPOSE["services"], "collector": {
        **COMPOSE["services"]["collector"],
        "environment": {"INTAKE_URL": "https://intake.example.org/ingest", "EXTRA": "https://elsewhere.example.net/x"},
    }}}
    problems = disclosure.drift(changed, disclosure.render(changed))
    assert any("elsewhere.example.net" in p for p in problems)


def test_a_new_declared_destination_needs_a_new_page():
    egress = COMPOSE["x-axiom-egress"] + [{"host": "new.example.org", "port": 443, "purpose": "p", "data": "d"}]
    problems = disclosure.drift({**COMPOSE, "x-axiom-egress": egress}, disclosure.render(COMPOSE))
    assert any("new.example.org" in p for p in problems)


def test_a_definition_without_a_policy_is_refused():
    with pytest.raises(disclosure.DisclosureError):
        disclosure.render({"services": COMPOSE["services"]})


def test_a_listener_given_as_address_and_port_together_is_read():
    """The node chart writes listeners as "address:port"."""
    chart_shape = {**COMPOSE, "services": {"collector": {**COMPOSE["services"]["collector"],
        "x-axiom-listen": [{"address": "127.0.0.1:8765", "purpose": "the read-only monitor page"}]}}}
    page = disclosure.render(chart_shape)
    assert "`127.0.0.1:8765`" in page and disclosure.drift(chart_shape, page) == []


# -- remote maintenance (ADR-183) ---------------------------------------------


def _with_maintenance(level):
    node = {**COMPOSE["x-axiom-node"], "maintenance": level}
    return {**COMPOSE, "x-axiom-node": node}


def test_the_page_says_how_much_remote_maintenance_the_site_accepts():
    page = disclosure.render(_with_maintenance("requests"))
    for expected in ('maintenance = "requests"', "signed", "allowlist", "approves",
                     "no account", "no port", "maintenance off"):
        assert expected in page, expected
    off = disclosure.render(_with_maintenance("off"))
    assert 'maintenance = "off"' in off and "Nothing remote runs" in off
    sessions = disclosure.render(_with_maintenance("sessions"))
    assert "support session" in sessions and "only a person at this site" in sessions


def test_an_undeclared_maintenance_level_reads_as_off():
    assert 'maintenance = "off"' in disclosure.render(COMPOSE)


def test_a_page_that_understates_the_maintenance_level_is_caught():
    stale = disclosure.render(_with_maintenance("off"))
    problems = disclosure.drift(_with_maintenance("sessions"), stale)
    assert any("maintenance" in p for p in problems), problems
    assert disclosure.drift(_with_maintenance("sessions"), disclosure.render(_with_maintenance("sessions"))) == []


def test_an_unknown_maintenance_level_is_not_disclosable():
    with pytest.raises(disclosure.DisclosureError):
        disclosure.render(_with_maintenance("anything"))
