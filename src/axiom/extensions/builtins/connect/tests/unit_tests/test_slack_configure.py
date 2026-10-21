# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

sc = pytest.importorskip("axiom.extensions.builtins.connect.skills.slack_configure")

def test_resolve_by_name():
    e=[{"id":"C1","name":"general"},{"id":"C2","name":"ops-channel"}]
    assert sc.resolve_channel_id(e,"ops-channel")=="C2"
    assert sc.resolve_channel_id(e,"#ops-channel")=="C2"

def test_resolve_by_id_passthrough():
    assert sc.resolve_channel_id([], "C0ABC")=="C0ABC"

def test_resolve_unknown_returns_none():
    assert sc.resolve_channel_id([{"id":"C1","name":"x"}],"nope") is None

def test_configure_with_fake_client_joins_and_verifies():
    class Fake:
        def auth_test(self): return {"ok":True,"team":"UT","user":"U-bot"}
        def conversations_list(self,**k): return {"channels":[{"id":"C2","name":"ops-channel"}]}
        def conversations_join(self,**k): self.joined=k["channel"]
    f=Fake()
    r=sc.configure({"bot_token":"xoxb","app_token":"xapp","channel":"ops-channel","web_client":f},ctx=None)
    assert r.ok and r.value["channel_id"]=="C2" and r.value["team"]=="UT"
