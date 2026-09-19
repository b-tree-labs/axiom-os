# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Templates for scaffolded AEOS extensions.

A :class:`Template` is a named starting point for ``axi ext init``. The
built-in compound template writes the canonical §5.1 layout; future
templates may emit narrower shapes (e.g. ``tool``-only, ``agent``-only)
or domain-specific starting points.

Third parties may add templates by registering under the
``axiom.ext.templates`` entry-point group (mirrors §11.3 for providers).
"""

from __future__ import annotations

import importlib.metadata as _md
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Template:
    """A scaffolding template for ``axi ext init``.

    Attributes:
        id: Stable identifier used on the CLI (``--template <id>``).
        description: One-line summary shown by ``axi ext templates``.
        create: Callable that materializes the template at a destination
            directory. Signature matches :func:`axiom.cli.ext.templates.scaffold.create`.
        is_default: Exactly one registered template must set this to True.
            That template is what ``axi ext init`` uses when no explicit
            ``--template`` is supplied.
    """

    id: str
    description: str
    create: Callable[..., None]
    is_default: bool = False


_ENTRY_POINT_GROUP = "axiom.ext.templates"


def _builtin_templates() -> list[Template]:
    from axiom.cli.ext.templates import scaffold as _scaffold

    return [
        Template(
            id="compound",
            description=(
                "Canonical AEOS §5.1 compound layout — all seven capability "
                "subdirectories, docs tree, and axiom-tests wiring"
            ),
            create=_scaffold.create,
            is_default=True,
        ),
    ]


def registry() -> list[Template]:
    """Return the list of available templates (built-ins + entry-point overrides).

    Entry-point registrations extend the registry; any template whose ``id``
    collides with a built-in replaces the built-in.
    """
    templates: dict[str, Template] = {t.id: t for t in _builtin_templates()}

    try:
        eps = _md.entry_points(group=_ENTRY_POINT_GROUP)
    except TypeError:  # pragma: no cover — pre-3.10 API
        eps = _md.entry_points().get(_ENTRY_POINT_GROUP, [])  # type: ignore[attr-defined]

    for ep in eps:
        try:
            obj = ep.load()
        except Exception:  # pragma: no cover — broken third-party install
            continue
        template = obj() if callable(obj) and not isinstance(obj, Template) else obj
        if not isinstance(template, Template):
            continue
        templates[template.id] = template

    return list(templates.values())


def get_template(template_id: str) -> Template | None:
    """Return the template with ``template_id`` or ``None`` if not registered."""
    for t in registry():
        if t.id == template_id:
            return t
    return None


def default_template() -> Template:
    """Return the registry's default template.

    Raises:
        RuntimeError: if the registry does not have exactly one default.
            This is a programming error rather than user input, so it is
            surfaced as an exception rather than a CLI exit code.
    """
    defaults = [t for t in registry() if t.is_default]
    if len(defaults) != 1:
        raise RuntimeError(
            f"template registry must have exactly one default; found {len(defaults)}"
        )
    return defaults[0]


__all__ = [
    "Template",
    "registry",
    "get_template",
    "default_template",
]
