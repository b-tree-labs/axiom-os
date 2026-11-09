# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.annotate`` — record metadata on a credential without replacing it.

``secrets rotate --expires-at`` is the expiry of a *replacement*. An operator
who already holds the credential and only learned its expiry from a console
must not be sent there: rotation issues a new value. This verb writes
``expires_at`` and ``notes`` and does not open the value.
"""

from __future__ import annotations

import re
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..foreign.store import ForeignCredentialStore

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = params.get("name")
    if not name:
        return SkillResult(ok=False, errors=["annotate needs a credential name"])
    expires_at = params.get("expires_at")
    notes = params.get("notes")
    host = params.get("host")
    if expires_at is None and notes is None and host is None:
        return SkillResult(ok=False, errors=[
            "annotate needs --expires-at, --host or --notes; it does not rotate",
        ])
    if expires_at is not None and not _DATE.match(str(expires_at)):
        return SkillResult(ok=False, errors=[
            "expires-at must be YYYY-MM-DD",
        ])
    fields: dict[str, str] = {}
    if expires_at is not None:
        fields["expires_at"] = str(expires_at)
    if notes is not None:
        fields["notes"] = str(notes)
    if host is not None:
        fields["host"] = str(host).strip().lower()
    store = params.get("_store") or ForeignCredentialStore(ctx.state_dir)
    try:
        meta = store.update_metadata(name, **fields)
    except (KeyError, ValueError) as exc:
        return SkillResult(ok=False, errors=[f"annotate failed: {exc}"])
    shown = {k: meta.get(k) for k in ("name", "expires_at", "host", "notes") if k in meta or k == "name"}
    return SkillResult(
        ok=True,
        value=shown,
        actions_taken=[f"recorded metadata on {name!r} without replacing the value"],
    )


__all__ = ["run"]
