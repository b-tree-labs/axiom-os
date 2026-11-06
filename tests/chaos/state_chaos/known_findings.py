# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Findings the chaos matrix has proven and that are not fixed yet.

Each (artifact, damage) cell listed here is a STRICT xfail: it must keep failing
until the fix lands, and the moment it is fixed the xfail turns into an XPASS
failure that forces this entry to be deleted. A finding cannot be forgotten,
and cannot be silently fixed without the table being updated.

See docs/working/state-chaos-findings.md for severity, cause and fix.
"""

from __future__ import annotations

KNOWN: dict[tuple[str, str], str] = {
    ('agents-consent', 'delete_file'): 'SC-03A',
    ('agents-consent', 'rename_away'): 'SC-03A',
    ('settings', 'delete_file'): 'SC-08A',
    ('settings', 'rename_away'): 'SC-08A',
    ('site-rules', 'delete_file'): 'SC-07A',
    ('site-rules', 'rename_away'): 'SC-07A',
}
