# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.git_credential`` — the git credential-helper protocol.

git invokes the configured helper as ``<helper> get|store|erase`` with
``key=value`` lines on stdin. ``get`` answers from the foreign-credential
store when a stored credential's ``git_host`` matches the request host —
so git never needs a token in config, remotes, or shell history.
``store``/``erase`` are acknowledged no-ops: custody stays with the
axiom store, not with git's own cache.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..foreign.store import ForeignCredentialStore

_OPS = ("get", "store", "erase")


def parse_credential_input(text: str) -> dict[str, str]:
    """Parse git's ``key=value`` request lines (blank line terminates)."""
    request: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            break
        if "=" in line:
            key, _, value = line.partition("=")
            request[key.strip()] = value.strip()
    return request


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    op = params.get("op")
    if op not in _OPS:
        return SkillResult(ok=False, errors=[
            f"unknown git-credential operation {op!r}; expected one of {_OPS}",
        ])

    if op in ("store", "erase"):
        # Custody never transfers to git's own credential cache.
        return SkillResult(ok=True, value={"output": ""})

    request = parse_credential_input(str(params.get("input") or ""))
    host = request.get("host", "")
    if not host:
        return SkillResult(ok=True, value={"output": ""})

    store = params.get("_store") or ForeignCredentialStore(ctx.state_dir)
    for meta in store.list():
        if meta.get("git_host") != host:
            continue
        username = (
            meta.get("git_username")
            or request.get("username")
            or "token"
        )
        try:
            with store.get(meta["name"]) as secret:
                password = secret.as_str()
        except (KeyError, RuntimeError) as exc:
            return SkillResult(ok=False, errors=[
                f"credential {meta['name']!r} is wired for {host} but its "
                f"value could not be read: {exc}",
            ])
        return SkillResult(ok=True, value={
            "output": f"username={username}\npassword={password}\n",
        })

    # No match: emit nothing so git falls through to its next helper.
    return SkillResult(ok=True, value={"output": ""})


__all__ = ["run", "parse_credential_input"]
