# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Skill-layer tests for press.mirror_* — the wrappers the CLI actually calls.

The engine has its own chaos suite; these exist because the wrappers can
break independently (they did: SkillResult(data=...) crashed every verb
while 335 engine tests stayed green)."""

from __future__ import annotations

import json
import logging

from axiom.extensions.builtins.publishing.skills import mirror_sync
from axiom.infra.skills import SkillContext


def _ctx(tmp_path):
    return SkillContext(registry=None, state_dir=tmp_path / "publisher",
                        logger=logging.getLogger("test.mirror"))


def test_add_returns_ok_skillresult_with_value(tmp_path):
    ctx = _ctx(tmp_path)
    doc = tmp_path / "doc.md"
    doc.write_text("hello\n")
    result = mirror_sync.add({"local": str(doc), "url": "https://x/share"}, ctx)
    assert result.ok
    assert result.value["name"] == "doc"
    registry = json.loads((tmp_path / "publisher" / "mirrors.json").read_text())
    assert registry["doc"]["url"] == "https://x/share"


def test_status_reports_registry_via_value(tmp_path):
    ctx = _ctx(tmp_path)
    doc = tmp_path / "doc.md"
    doc.write_text("hello\n")
    mirror_sync.add({"local": str(doc), "url": "https://x/share"}, ctx)
    result = mirror_sync.status({}, ctx)
    assert result.ok
    assert result.value["mirrors"]["doc"]["url"] == "https://x/share"


def test_sync_with_empty_registry_is_a_clean_error(tmp_path):
    result = mirror_sync.sync({}, _ctx(tmp_path))
    assert not result.ok
    assert result.errors


def test_msal_scopes_contain_no_reserved_scopes():
    """MSAL raises on any reserved scope in the request list (it appends
    them itself); 'offline_access' in SCOPES broke every device flow."""
    from axiom.extensions.builtins.publishing.providers import sharepoint
    assert not set(sharepoint.SCOPES) & {"openid", "offline_access", "profile"}


def test_resolve_clears_a_blocked_conflict_end_to_end(tmp_path):
    """The wrapper path a person hits: sync surfaces a blocked conflict with
    guidance, status shows it blocked, resolve clears it and it converges."""
    from axiom.extensions.builtins.publishing.providers.local_editor import (
        LocalFileEditor,
    )

    ctx = _ctx(tmp_path)
    remote = tmp_path / "remote.md"
    mirror = tmp_path / "mirror.md"
    LocalFileEditor(path=str(remote)).human_save("origin\n")  # remote v1
    # local editor: explicit vendor + a plain filesystem path as the "remote"
    mirror_sync.add(
        {"local": str(mirror), "url": str(remote), "name": "m",
         "vendor": "local"}, ctx)
    assert mirror_sync.sync({"name": "m"}, ctx).ok            # baseline pull
    assert mirror.read_text() == "origin\n"

    # both sides move to different content → a blocked conflict
    LocalFileEditor(path=str(remote)).human_save("remote moved\n")  # v2
    mirror.write_text("local moved\n")
    rep = mirror_sync.sync({"name": "m"}, ctx).value["reports"]["m"]
    assert rep["action"] == "conflict"
    assert "guidance" in rep and "paused" in rep["guidance"]

    st = mirror_sync.status({}, ctx).value["mirrors"]["m"]
    assert st["blocked"] is True and "guidance" in st

    res = mirror_sync.resolve({"name": "m", "strategy": "theirs"}, ctx)
    assert res.ok and res.value["report"]["action"] == "resolved"
    assert mirror.read_text() == "remote moved\n"
    assert mirror_sync.sync({"name": "m"}, ctx).value["reports"]["m"]["action"] == "noop"


def test_resolve_validates_params(tmp_path):
    ctx = _ctx(tmp_path)
    assert not mirror_sync.resolve({"strategy": "theirs"}, ctx).ok  # missing name
    bad = mirror_sync.resolve({"name": "x", "strategy": "nope"}, ctx)
    assert not bad.ok and "theirs, ours, merged" in bad.errors[0]
    unknown = mirror_sync.resolve({"name": "ghost", "strategy": "ours"}, ctx)
    assert not unknown.ok and "unknown mirror" in unknown.errors[0]
