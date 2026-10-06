# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Document-editor connector kind (ADR-110 §Decision-5, option B).

A *versioned document editor* is the mirror's remote side: ``read`` the current
document, ``write`` it **with an expected version** (optimistic concurrency, the
never-write-blind invariant), and list ``versions``. That conditional write is
why this is its own connector kind rather than the bulk file-store
``StorageConnectorProvider`` — the file-store Protocol has no expected-version.

This registry (built on the one shared ``ConnectorRegistry`` mechanism, ADR-110
§Decision-1) maps a vendor to a factory that returns a ``RemoteEditorEndpoint``.
The mirror resolves its endpoint through ``get_editor`` instead of instantiating
a hardcoded class, so a new backend (Google Docs, an S3 object-lock editor) is
one ``register_editor`` call with nothing above it changing.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.connector_registry import ConnectorRegistry

_EDITORS: ConnectorRegistry = ConnectorRegistry("document editor")


def register_editor(vendor: str, factory: Any, *, replace: bool = False) -> None:
    """Register a ``factory(**config) -> RemoteEditorEndpoint`` under ``vendor``."""
    _EDITORS.register(vendor, factory, replace=replace)


def available_editors() -> tuple[str, ...]:
    return _EDITORS.available()


def get_editor(vendor: str, **config: Any):
    """Return a ``RemoteEditorEndpoint`` for ``vendor`` (raises on an unknown one)."""
    return _EDITORS.create(vendor, **config)


def _routes() -> list[dict]:
    """URL→vendor routes: config-dir overrides first, then packaged defaults.

    DATA, not code — read at call time so an operator adds a backend by editing
    ``<config_dir>/editor-routing.toml`` with no code patch and no restart."""
    from importlib.resources import files

    from axiom.infra.config import default_config_dir
    from axiom.infra.toml_compat import load_toml, tomllib

    routes: list[dict] = []
    override = default_config_dir() / "editor-routing.toml"
    if override.exists():
        routes.extend(load_toml(override).get("route", []) or [])
    with (files(__package__) / "editor_routing.toml").open("rb") as fh:
        routes.extend(tomllib.load(fh).get("route", []) or [])
    return routes


def resolve_editor_vendor(url: str, explicit: str | None = None) -> str:
    """Which editor kind serves ``url``. An explicit vendor always wins; else
    the first matching route (config) decides. No hardcoded default — an
    unroutable URL raises, naming the fix, rather than silently assuming one."""
    if explicit:
        return explicit
    for route in _routes():
        needle = route.get("contains")
        if needle and needle in url:
            return route["vendor"]
    known = ", ".join(available_editors()) or "(none)"
    raise ValueError(
        f"no editor route for URL {url!r}; registered editors: {known}. "
        f"Add a route to editor-routing.toml (no code patch) or pass vendor=."
    )


def _onedrive_editor(*, url: str, **_: Any):
    # Lazy import: GraphEditorEndpoint pulls msal/requests; keep it off the
    # module-load path so registering the kind is cheap.
    from .sharepoint import GraphEditorEndpoint

    return GraphEditorEndpoint.from_share_url(url)


def _file_url_to_path(url: str) -> str:
    """Resolve a ``file://`` URL to a local filesystem path; return a plain
    path unchanged.

    The local editor's "remote" is a filesystem path, but the ``file://`` route
    in ``editor_routing.toml`` hands this factory a URL — a verbatim ``file://…``
    would point the editor at a nonexistent file. A plain path (the explicit
    ``vendor=\"local\"`` convention) is returned untouched; a ``file://`` URL is
    parsed to its path and percent-decoded (so ``%20`` becomes a space).
    """
    if not url.startswith("file:"):
        return url
    from urllib.parse import unquote, urlparse

    return unquote(urlparse(url).path) or url


def _local_editor(*, url: str, **_: Any):
    # A file-backed versioned editor — the second kind, proving a new backend is
    # one register call. The remote is a filesystem path; a file:// URL (the
    # editor_routing.toml route) is resolved to that path, a plain path used as-is.
    from .local_editor import LocalFileEditor

    return LocalFileEditor(path=_file_url_to_path(url))


# Built-in: OneDrive / SharePoint via Microsoft Graph (If-Match optimistic
# concurrency). This is the same GraphEditorEndpoint the mirror always used —
# now a registered kind, not a hardcoded class.
register_editor("onedrive", _onedrive_editor)
# Built-in: a local-file versioned editor (no cloud) — portability proof + a way
# to mirror to a shared-drive path or exercise the mirror offline.
register_editor("local", _local_editor)


__all__ = ["available_editors", "get_editor", "register_editor", "resolve_editor_vendor"]
