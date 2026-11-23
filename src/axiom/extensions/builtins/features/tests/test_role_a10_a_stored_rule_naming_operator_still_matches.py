# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ROLE-A10: switchboard records written before ADR-261 that name ``operator`` keep working.

A cohort rule, a rollout match or an approval policy stored on a node says ``operator``.
The role is ``site-admin`` now, and nothing rewrites the stored record, so the record
must still match a site admin, and a record naming ``site-admin`` must still match a
session whose grant was stored as ``operator``.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.features import approvals as ap
from axiom.extensions.builtins.features.flag_registry import ApprovalPolicy
from axiom.extensions.builtins.features.tests.conftest import svc


@pytest.mark.parametrize("stored, held", [
    ("operator", "site-admin"), ("site-admin", "operator"), ("operator", "operator"),
])
def test_role_a10_a_cohort_rule_matches_either_spelling(loop, stored, held):
    ts = loop["targeting"]
    ts.put_cohort("admins", rules={"attr": "role", "op": "contains", "value": stored}, by="@t")
    ts.put_rollout("beta_editor", "development", rules=[{"cohort": "admins", "value": "on"}],
                   reason="pilot", by="@t")
    s = svc()
    assert s.resolve_flag("beta_editor", user="zed", roles=[held]) == "on"
    assert s.resolve_flag("beta_editor", user="zed", roles=["viewer"]) == "off"


def test_role_a10_an_approval_policy_naming_operator_counts_a_site_admin():
    policy = ApprovalPolicy(approvals=1, roles=("operator",))
    approval = [{"by": "@bea:site", "roles": ["site-admin"]}]
    assert ap.counted(policy, approval, author="@ana:site") == 1
    assert "Site admin" in ap.describe(policy)
