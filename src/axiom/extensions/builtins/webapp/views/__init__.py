# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A saved view: what somebody had configured, kept for when they come back.

A surface a person configures and loses is a surface they configure once and
then stop using. Picking four channels across two feeds, setting a window and
turning on a comparison is real work, and it should survive a reload, a
laptop and a week.

## Why the document is opaque

The platform stores a JSON document per (principal, surface, name) and never
reads inside it. What belongs in a chart's saved state is a question about
charts, and what belongs in a table's is a question about tables; a schema
here would make the platform learn both and break whenever either changed.

The consumer that wrote the document is the one that reads it, and it is
responsible for a document it does not recognise. Storing an opaque blob is
the honest shape of "remember what I had".

## Why it is per PRINCIPAL

Two people looking at one deployment are not looking at the same question.
A view is personal by default, and a view somebody wants to hand to a
colleague is a different feature with its own sharing decision to make.
"""

from .labels import default_label, default_labels
from .models import ChannelLabel, SavedView
from .store import delete_view, list_views, read_labels, read_view, write_label, write_view

__all__ = [
    "ChannelLabel",
    "SavedView",
    "default_label",
    "default_labels",
    "delete_view",
    "list_views",
    "read_labels",
    "read_view",
    "write_label",
    "write_view",
]
