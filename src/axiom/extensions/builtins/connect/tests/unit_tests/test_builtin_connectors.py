# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Built-in comms connector descriptors live in the connect extension (not
core). register_builtin_connectors populates a fabric."""
from __future__ import annotations

from axiom.extensions.builtins.connect.connectors import (
    imessage_connector_descriptor,
    register_builtin_connectors,
    slack_connector_descriptor,
    teams_connector_descriptor,
)
from axiom.infra.connector_fabric import Availability, InProcessConnectorFabric


def test_slack_descriptor_well_formed():
    d = slack_connector_descriptor()
    assert d.name == "ai.axiom.connector.slack" and d.availability is Availability.AVAILABLE
    assert d.setup.install_kind == "app_manifest"
    assert d.setup.remedy_for("missing_scope")  # error guidance present


def test_planned_connectors():
    assert teams_connector_descriptor().availability is Availability.PLANNED
    assert imessage_connector_descriptor().meta["ai.axiom.registry/requires_mac_endpoint"] == "true"


def test_register_builtins_populates_a_fabric():
    fab = InProcessConnectorFabric()
    register_builtin_connectors(fab)
    names = {d.name for d in fab.catalog()}
    assert {"ai.axiom.connector.slack", "ai.axiom.connector.teams",
            "ai.axiom.connector.sms", "ai.axiom.connector.imessage"} <= names
