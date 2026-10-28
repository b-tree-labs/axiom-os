# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Document-editor connector kind (ADR-110 §Decision-5, option B).

The mirror's remote side is a *versioned document editor* (read / write-with-
expected-version / versions) — distinct from the bulk file-store
StorageConnectorProvider, because it needs optimistic concurrency the file-store
Protocol does not carry. This is that kind's registry: one factory per vendor,
so a new backend (Google Docs, S3-with-object-lock) is register-one-factory, and
the mirror resolves its endpoint through it instead of a hardcoded class."""

from __future__ import annotations

from axiom.extensions.builtins.publishing.providers import editors


class _FakeEditor:
    def __init__(self, *, url):
        self.url = url

    def read(self):  # RemoteEditorEndpoint shape
        return None

    def write(self, text, expected_version):
        return "1"

    def versions(self, limit=10):
        return []


def test_onedrive_editor_is_registered_by_default():
    assert "onedrive" in editors.available_editors()


def test_register_and_resolve_a_new_vendor_is_one_factory():
    editors.register_editor("gdocs-fake", _FakeEditor, replace=True)
    try:
        assert "gdocs-fake" in editors.available_editors()
        ep = editors.get_editor("gdocs-fake", url="https://drive/doc")
        assert isinstance(ep, _FakeEditor) and ep.url == "https://drive/doc"
    finally:
        editors._EDITORS.unregister("gdocs-fake")


def test_unknown_vendor_raises_listing_known():
    import pytest
    with pytest.raises(ValueError, match="document editor 'zoho'"):
        editors.get_editor("zoho", url="x")


def test_mirror_engine_resolves_endpoint_through_the_registry(tmp_path):
    """The mirror no longer hardcodes GraphEditorEndpoint: _engine_for resolves
    the editor kind from the registry, so any registered vendor threads through
    and the engine keeps its RemoteEditorEndpoint contract (optimistic writes)."""
    import logging

    from axiom.extensions.builtins.publishing.skills import mirror_sync
    from axiom.infra.skills import SkillContext

    editors.register_editor("fake-v", _FakeEditor, replace=True)
    try:
        ctx = SkillContext(registry=None, state_dir=tmp_path,
                           logger=logging.getLogger("t"))
        engine = mirror_sync._engine_for(
            "m", {"local": str(tmp_path / "d.md"), "url": "u://x",
                  "vendor": "fake-v"}, ctx)
        assert isinstance(engine.endpoint, _FakeEditor)
        assert engine.endpoint.url == "u://x"
    finally:
        editors._EDITORS.unregister("fake-v")


# --- URL→vendor routing is config, not a baked default (ADR-110 §Decision-6) ---

def test_sharepoint_url_routes_to_onedrive_by_config():
    assert editors.resolve_editor_vendor(
        "https://x.sharepoint.com/personal/u/Documents/d.md") == "onedrive"


def test_explicit_vendor_always_wins():
    assert editors.resolve_editor_vendor("https://anything/x", "gdocs") == "gdocs"


def test_unroutable_url_raises_no_silent_default():
    import pytest
    with pytest.raises(ValueError, match="no editor route"):
        editors.resolve_editor_vendor("https://unknown.example/doc")


def test_deployment_override_adds_a_route_without_a_code_patch(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_CONFIG_DIR", str(tmp_path))
    (tmp_path / "editor-routing.toml").write_text(
        '[[route]]\ncontains = "drive.google.com"\nvendor = "gdocs"\n')
    # brand-new backend routed by config alone; packaged defaults still apply
    assert editors.resolve_editor_vendor("https://drive.google.com/d/1") == "gdocs"
    assert editors.resolve_editor_vendor("https://x.sharepoint.com/d") == "onedrive"


# --- the local editor resolves file:// URLs, not just plain paths ---
# (regression: the file:// route in editor_routing.toml used to hand the local
# editor a verbatim "file://…", which is not a real path — so the route was dead.)

def _seed_local(path, text):
    from axiom.extensions.builtins.publishing.providers.local_editor import (
        LocalFileEditor,
    )
    LocalFileEditor(path=str(path)).human_save(text)


def test_local_editor_resolves_a_file_url_to_the_real_path(tmp_path):
    doc = tmp_path / "note.md"
    _seed_local(doc, "hello from a file url\n")
    ep = editors.get_editor("local", url=f"file://{doc}")   # file:///abs/...
    assert ep.read().text == "hello from a file url\n"


def test_local_editor_still_accepts_a_plain_path(tmp_path):
    doc = tmp_path / "note.md"
    _seed_local(doc, "plain path still works\n")
    ep = editors.get_editor("local", url=str(doc))
    assert ep.read().text == "plain path still works\n"


def test_local_editor_percent_decodes_a_file_url(tmp_path):
    from urllib.parse import quote
    sub = tmp_path / "with space"
    sub.mkdir()
    doc = sub / "note.md"
    _seed_local(doc, "spaces are fine\n")
    ep = editors.get_editor("local", url="file://" + quote(str(doc)))
    assert ep.read().text == "spaces are fine\n"


def test_file_url_routes_to_local_and_reads_end_to_end(tmp_path):
    doc = tmp_path / "note.md"
    _seed_local(doc, "routed and read\n")
    url = f"file://{doc}"
    assert editors.resolve_editor_vendor(url) == "local"   # editor_routing.toml
    assert editors.get_editor("local", url=url).read().text == "routed and read\n"
