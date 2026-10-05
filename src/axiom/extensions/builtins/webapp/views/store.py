# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Reading and writing a saved view, through the webapp session (ADR-052)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from axiom.extensions.builtins.webapp.catalog.store import session_scope
from axiom.extensions.builtins.webapp.views.models import LAST, ChannelLabel, SavedView

#: Beyond this a "saved view" is somebody storing their data in the state
#: store. Refused by size rather than by shape, because the shape is the
#: consumer's business and the size is the platform's.
MAX_DOCUMENT_BYTES = 64 * 1024

#: Beyond this a principal is accumulating views rather than keeping them.
MAX_VIEWS_PER_SURFACE = 50


class ViewTooLarge(ValueError):
    """The document is bigger than a view has any business being."""


class TooManyViews(ValueError):
    """This principal already holds as many views of this surface as allowed."""


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def read_view(principal: str, surface: str, name: str = LAST) -> dict[str, Any] | None:
    """The document, or None. A view that was never saved is not an error."""
    with session_scope() as session:
        row = session.get(SavedView, (principal, surface, name))
        if row is None:
            return None
        try:
            return json.loads(row.document)
        except json.JSONDecodeError:
            # Written by a version that is gone, or by hand. A surface that
            # cannot read its own saved state should open fresh rather than
            # refuse to open, which is the difference between a lost setting
            # and a lost page.
            return None


def list_views(principal: str, surface: str) -> list[dict[str, Any]]:
    """Every view this principal holds of this surface, newest first."""
    with session_scope() as session:
        rows = (
            session.query(SavedView)
            .filter(SavedView.principal == principal, SavedView.surface == surface)
            .all()
        )
        return sorted(
            (
                {
                    "name": r.name,
                    "pinned": bool(r.pinned),
                    "updated_at": r.updated_at.isoformat() if r.updated_at else "",
                }
                for r in rows
            ),
            key=lambda v: v["updated_at"],
            reverse=True,
        )


def write_view(
    principal: str,
    surface: str,
    document: dict[str, Any],
    *,
    name: str = LAST,
    pinned: bool | None = None,
) -> None:
    """Save it, replacing whatever was under that name."""
    if not principal:
        raise ValueError("a view belongs to somebody; principal is required")
    body = json.dumps(document, separators=(",", ":"), sort_keys=True)
    if len(body.encode()) > MAX_DOCUMENT_BYTES:
        raise ViewTooLarge(
            f"a saved view may be {MAX_DOCUMENT_BYTES} bytes; this one is "
            f"{len(body.encode())}"
        )
    with session_scope() as session:
        row = session.get(SavedView, (principal, surface, name))
        if row is None:
            held = (
                session.query(SavedView)
                .filter(SavedView.principal == principal, SavedView.surface == surface)
                .count()
            )
            if held >= MAX_VIEWS_PER_SURFACE:
                raise TooManyViews(
                    f"{principal} already holds {held} views of {surface!r}; "
                    "delete one before saving another"
                )
            row = SavedView(principal=principal, surface=surface, name=name)
            session.add(row)
        row.document = body
        if pinned is not None:
            row.pinned = pinned
        row.updated_at = _now()
        session.commit()


def delete_view(principal: str, surface: str, name: str) -> bool:
    """Remove it. Returns whether there was one."""
    with session_scope() as session:
        row = session.get(SavedView, (principal, surface, name))
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True


#: A label longer than this is a sentence, and a chip cannot hold a sentence.
MAX_LABEL = 64


class LabelRefused(ValueError):
    """The label is empty, or long enough to be a description."""


def read_labels(site: str) -> dict[str, str]:
    """``{"feed:channel": label}`` for one site.

    One query per site rather than per channel: a picker asks for all of them
    at once, and a figure asks for the handful it is drawing out of the same
    answer.
    """
    with session_scope() as session:
        rows = (
            session.query(ChannelLabel).filter(ChannelLabel.site == site).all()
        )
        return {f"{r.feed}:{r.channel}": r.label for r in rows}


def write_label(
    site: str, feed: str, channel: str, label: str, *, set_by: str = ""
) -> None:
    """Name a channel, or clear the name with an empty one."""
    label = label.strip()
    if len(label) > MAX_LABEL:
        raise LabelRefused(
            f"a channel name may be {MAX_LABEL} characters; this one is {len(label)}"
        )
    with session_scope() as session:
        row = session.get(ChannelLabel, (site, feed, channel))
        if not label:
            # Clearing it returns the channel to its default, which is derived
            # rather than stored — so the row goes rather than holding "".
            if row is not None:
                session.delete(row)
                session.commit()
            return
        if row is None:
            row = ChannelLabel(site=site, feed=feed, channel=channel, label=label)
            session.add(row)
        row.label = label
        row.set_by = set_by
        row.updated_at = _now()
        session.commit()
