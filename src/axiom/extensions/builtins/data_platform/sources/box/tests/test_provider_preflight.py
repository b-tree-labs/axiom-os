# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""BoxSourceProvider.preflight — live verification with plain-language fixes."""

from __future__ import annotations

from unittest.mock import patch

from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
    ConnectorConfig,
)
from axiom.extensions.builtins.data_platform.sources.box.provider import (
    BoxSourceProvider,
)


def _cfg():
    return ConnectorConfig(
        name="dmsr", kind="box", bronze_root="/tmp/bronze",
        params={"folder_id": "228326101313"},
    )


class _FakeClient:
    def __init__(self, *, me=None, folder=None, folder_exc=None):
        self._me, self._folder, self._folder_exc = me, folder, folder_exc

    def get_json(self, path, *a, **k):
        if path == "/users/me":
            return self._me
        if self._folder_exc:
            raise self._folder_exc
        return self._folder


def _run(client):
    with patch.object(BoxSourceProvider, "_resolve_jwt_auth", return_value=None), \
         patch.object(BoxSourceProvider, "_resolve_session_dir", return_value="/tmp/x"), \
         patch(
            "axiom.extensions.builtins.data_platform.sources.box.provider.BoxSessionApiClient",
            return_value=client,
         ):
        return BoxSourceProvider().preflight(_cfg())


def test_all_green_when_authed_and_folder_visible():
    res = _run(_FakeClient(
        me={"login": "AutomationUser_9@boxdevedition.com"},
        folder={"name": "DMSR Initiative", "item_collection": {"total_count": 1038}},
    ))
    assert res.ok
    assert "DMSR Initiative" in res.checks[-1].message


def test_folder_denied_yields_collaborator_remediation():
    res = _run(_FakeClient(
        me={"login": "AutomationUser_9@boxdevedition.com"},
        folder_exc=RuntimeError("Box GET /folders/x failed: 403 forbidden"),
    ))
    assert not res.ok
    fix = res.blockers[0]
    assert fix.name == "Folder access"
    assert "AutomationUser_9@boxdevedition.com" in fix.remediation
    assert fix.copy_value == "AutomationUser_9@boxdevedition.com"


def test_folder_check_failure_when_authed():
    res = _run(_FakeClient(me={"login": "x"}, folder_exc=RuntimeError("401")))
    assert not res.ok
    assert res.checks[0].ok       # authentication succeeded
    assert not res.checks[-1].ok  # folder check failed
