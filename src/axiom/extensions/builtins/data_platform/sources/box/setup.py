# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Guided setup and live verification for a Box connector (client-credentials app).

``axi data connector add box`` and ``axi data connector test`` both run
:func:`verify`. It talks to Box exactly as the scheduled pull does (the same
:class:`BoxCcgAuth`, the same :class:`BoxSessionApiClient`, the same
:class:`BoxIngestSource` walk), so a connector that passes here is one the pull
can read:

1. **Sign in.** Exchange the app's client id, secret and enterprise id for a token,
   then ask Box who that token is (the app's service account).
2. **Folder.** Open the folder by id, or list the folders the service account can
   see so the person picks one.
3. **One file.** Fetch one file's metadata (name, size, sha1, modified).
4. **Dry-run pull.** Walk the folder the way the pull walks it and route every
   file through the connector's provenance rules, without fetching a byte or
   writing anything: what would land, where.

Every failure carries a cause and a fix a non-expert can act on. Box's own error
codes (``invalid_client``, ``unauthorized_client``, ``invalid_grant``,
``not_found``) and the network failures (proxy, TLS interception, no route) are
told apart, because each has a different fix and a different person who can make
it. Nothing here prints, logs or returns a secret.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .ccg_auth import BoxCcgAuth, BoxCcgConfig

#: How many files the dry-run lists by name; the rest are counted.
DRY_RUN_SHOWN = 10


@dataclass
class Step:
    """One verification step, in the order a person would check it by hand."""

    name: str
    ok: bool
    detail: str
    cause: str = ""
    fix: str = ""
    who: str = "you"  # "you" or "admin": who can make the fix

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "cause": self.cause, "fix": self.fix, "who": self.who}


@dataclass
class Verification:
    """What :func:`verify` found. ``ok`` only when every step passed."""

    steps: list[Step] = field(default_factory=list)
    account: str = ""
    folder: dict[str, Any] = field(default_factory=dict)
    sample: dict[str, Any] | None = None
    would_land: list[dict[str, Any]] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "steps": [s.as_dict() for s in self.steps],
            "account": self.account,
            "folder": self.folder,
            "sample_file": self.sample,
            "dry_run": {"would_land": self.would_land[:DRY_RUN_SHOWN], "totals": self.totals},
        }


# ---------------------------------------------------------------- diagnosis


def _network_cause(exc: BaseException) -> tuple[str, str] | None:
    """(cause, fix) for a failure to reach Box at all, else None."""
    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a hard dependency
        return None
    if isinstance(exc, requests.exceptions.ProxyError):
        return ("network: the HTTP proxy refused or dropped the connection to Box",
                "check HTTPS_PROXY / https_proxy on this machine (unset it if there is no proxy), "
                "then try `curl -sI https://api.box.com` from the same shell")
    if isinstance(exc, requests.exceptions.SSLError):
        return ("network: TLS to Box failed, usually a proxy that re-signs certificates",
                "point REQUESTS_CA_BUNDLE at your organisation's CA bundle, or ask IT to "
                "exempt api.box.com, upload.box.com and dl.boxcloud.com from inspection")
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return ("network: this machine cannot reach Box",
                "allow outbound HTTPS (443) to api.box.com, upload.box.com and dl.boxcloud.com; "
                "if you are behind a proxy, set HTTPS_PROXY")
    return None


def diagnose_sign_in(exc: BaseException) -> tuple[str, str, str]:
    """(cause, fix, who) for a failed client-credentials token exchange."""
    net = _network_cause(exc)
    if net:
        return (*net, "you")
    text = str(exc)
    if "invalid_client" in text:
        return ("Box rejected the client id or client secret",
                "copy both again from the Box Developer Console: your app > Configuration > "
                "OAuth 2.0 Credentials (the secret needs 'Fetch Client Secret'); if unsure, "
                "reset the secret there and run this again", "you")
    if "unauthorized_client" in text:
        return ("the app is not allowed to sign in as the enterprise",
                "in the Developer Console set Authentication Method to 'Client Credentials Grant' "
                "and App Access Level to 'App Access Only' (or App + Enterprise), save, then have "
                "a Box admin authorize the app: Admin Console > Integrations > Platform Apps "
                "Manager > Add App, paste the client id", "admin")
    if "invalid_grant" in text:
        return ("Box did not accept the enterprise id for this app",
                "check the enterprise id (Developer Console: your app > General Settings, "
                "'Enterprise ID'); if it is right, the app has not been authorized in that "
                "enterprise yet: Admin Console > Integrations > Platform Apps Manager > Add App",
                "admin")
    return (f"Box sign-in failed: {_short(text)}",
            "check the client id, secret and enterprise id, and that a Box admin authorized the app",
            "you")


def diagnose_folder(exc: BaseException, folder_id: str, account: str) -> tuple[str, str, str]:
    """(cause, fix, who) for a folder the signed-in app cannot open."""
    net = _network_cause(exc)
    if net:
        return (*net, "you")
    text = str(exc)
    status = _status_of(text)
    who = account or "the app's service account"
    if status in (403, 404) or "not_found" in text or "access_denied" in text:
        return (f"the app is signed in but cannot see folder {folder_id}",
                f"in Box, open the folder > Share > Invite People, invite {who} as Viewer "
                "(or Editor if the pull should write receipts), then run this again. "
                "Check the folder id is the number at the end of the folder's Box address.",
                "you")
    if status == 401:
        return ("Box revoked the app's token",
                "a Box admin may have disabled or re-scoped the app: Admin Console > "
                "Integrations > Platform Apps Manager, re-authorize it", "admin")
    return (f"Box refused the folder request: {_short(text)}",
            "check the folder id and run this again", "you")


def _status_of(text: str) -> int | None:
    m = re.search(r"failed: (\d{3})\b", text)
    return int(m.group(1)) if m else None


def _short(text: str) -> str:
    return text if len(text) <= 160 else text[:157] + "..."


# ---------------------------------------------------------------- clients


def auth_for(cfg: BoxCcgConfig) -> BoxCcgAuth:
    """The client-credentials auth the scheduled pull builds from the same three values."""
    return BoxCcgAuth(cfg)


def _client(auth: Any):
    """The API client the scheduled pull builds, minus any SSO cookies."""
    from .session_api import BoxSessionApiClient

    # A path that holds no cookies: a client-credentials app authenticates by token
    # alone, and verification must not borrow somebody's browser session.
    return BoxSessionApiClient(session_dir=Path("/nonexistent-box-session"), jwt_auth=auth)


def sign_in(auth: Any) -> tuple[Any, Step, str]:
    """Mint a token and ask Box who it is. Returns (api or None, step, account login).

    ``auth`` is any Box auth the pull accepts (client credentials, JWT, OAuth).
    """
    api = _client(auth)
    try:
        auth.get_access_token()
    except Exception as exc:  # noqa: BLE001 - every failure becomes a diagnosed step
        cause, fix, who = diagnose_sign_in(exc)
        return None, Step("Sign in to Box", False, cause, cause, fix, who), ""
    try:
        me = api.get_json("/users/me", {"fields": "login,name"}) or {}
    except Exception as exc:  # noqa: BLE001
        cause, fix, who = diagnose_sign_in(exc)
        return None, Step("Sign in to Box", False, cause, cause, fix, who), ""
    login = str(me.get("login") or me.get("name") or "")
    return api, Step("Sign in to Box", True, f"signed in as {login or 'the app service account'}"), login


def list_folders(api: Any, parent: str = "0") -> list[dict[str, str]]:
    """The folders directly under ``parent`` that the signed-in account can see.

    For a client-credentials app, folder ``0`` is the service account's own root,
    where every folder shared with it appears.
    """
    out: list[dict[str, str]] = []
    params: dict[str, Any] = {"fields": "type,id,name", "limit": 1000, "usemarker": "true"}
    seen: set[str] = set()
    while True:
        page = api.get_json(f"/folders/{parent}/items", params) or {}
        for e in page.get("entries", []):
            if e.get("type") == "folder":
                out.append({"id": str(e.get("id")), "name": str(e.get("name") or e.get("id"))})
        marker = page.get("next_marker")
        if not marker or marker in seen:
            return out
        seen.add(marker)
        params = {**params, "marker": marker}


def open_folder(api: Any, folder_id: str, account: str) -> tuple[dict[str, Any], Step]:
    try:
        f = api.get_json(f"/folders/{folder_id}", {"fields": "id,name,item_collection"}) or {}
    except Exception as exc:  # noqa: BLE001
        cause, fix, who = diagnose_folder(exc, folder_id, account)
        return {}, Step("Open the folder", False, cause, cause, fix, who)
    name = str(f.get("name") or folder_id)
    count = (f.get("item_collection") or {}).get("total_count")
    info = {"id": folder_id, "name": name, "items": count}
    shown = f"{count} items at the top level" if count is not None else "contents not counted"
    return info, Step("Open the folder", True, f"'{name}' ({folder_id}), {shown}")


# ---------------------------------------------------------------- verification


def verify(
    auth: Any,
    *,
    connector_name: str,
    folder_id: str,
    bronze_root: str,
    params: dict[str, str] | None = None,
    default_disposition: str = "quarantine",
    default_tier: str | None = None,
    provenance_rules_file: str | None = None,
    api: Any = None,
    account: str = "",
    sign_in_step: Step | None = None,
) -> Verification:
    """Run every live check. Pass ``api`` (and the sign-in step) to reuse a sign-in."""
    v = Verification()
    if api is None:
        api, step, account = sign_in(auth)
        v.steps.append(step)
        if api is None:
            return v
    elif sign_in_step is not None:
        v.steps.append(sign_in_step)
    v.account = account

    folder, step = open_folder(api, folder_id, account)
    v.steps.append(step)
    if not step.ok:
        return v
    v.folder = folder

    from .source import (
        BoxIngestSource,
        exclude_extensions_from_params,
        exclude_prefixes_from_params,
        max_item_bytes_from_params,
    )

    p = params or {}
    source = BoxIngestSource(
        name=connector_name, folder_id=folder_id, api_client=api,
        exclude_prefixes=exclude_prefixes_from_params(p),
        exclude_extensions=exclude_extensions_from_params(p),
        max_item_bytes=max_item_bytes_from_params(p),
    )
    try:
        items = source.catalog(None)
    except Exception as exc:  # noqa: BLE001
        cause, fix, who = diagnose_folder(exc, folder_id, account)
        v.steps.append(Step("Read one file", False, cause, cause, fix, who))
        return v

    if not items:
        v.steps.append(Step(
            "Read one file", True,
            "the folder holds no files yet; the pull will land files as they arrive",
        ))
    else:
        first = items[0]
        try:
            meta = api.get_json(f"/files/{first.item_id}",
                                {"fields": "id,name,size,sha1,modified_at"}) or {}
        except Exception as exc:  # noqa: BLE001
            cause, fix, who = diagnose_folder(exc, folder_id, account)
            v.steps.append(Step("Read one file", False, cause, cause, fix, who))
            return v
        v.sample = {
            "id": str(meta.get("id") or first.item_id),
            "name": meta.get("name") or first.display_name,
            "path": first.source_path,
            "bytes": meta.get("size", first.size),
            "sha1": meta.get("sha1"),
            "modified_at": meta.get("modified_at"),
        }
        v.steps.append(Step(
            "Read one file", True,
            f"{v.sample['name']} ({_human_bytes(v.sample['bytes'])}, sha1 {str(v.sample['sha1'])[:12]})",
        ))

    v.would_land, v.totals = dry_run(
        items, connector_name=connector_name, bronze_root=bronze_root,
        default_disposition=default_disposition, default_tier=default_tier,
        provenance_rules_file=provenance_rules_file,
    )
    t = v.totals
    v.steps.append(Step(
        "Dry-run pull", True,
        f"{t['files']} files ({_human_bytes(t['bytes'])}) would land: "
        f"{t['allow']} allowed, {t['quarantine']} quarantined, {t['exclude']} excluded; nothing written",
    ))
    return v


def dry_run(
    items: list[Any],
    *,
    connector_name: str,
    bronze_root: str,
    default_disposition: str,
    default_tier: str | None,
    provenance_rules_file: str | None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Route each cataloged file the way the bronze writer would, writing nothing."""
    from axiom.rag.ingest_router import (
        Disposition,
        load_rules_file,
        route_path,
        with_non_document_rules,
    )

    rules = with_non_document_rules(load_rules_file(provenance_rules_file) if provenance_rules_file else [])
    default = Disposition(default_disposition)
    day = datetime.now(UTC).date().isoformat()
    subdir = {Disposition.ALLOW: "_records", Disposition.QUARANTINE: "_quarantine",
              Disposition.EXCLUDE: "_excluded"}
    totals = {"files": 0, "bytes": 0, "allow": 0, "quarantine": 0, "exclude": 0}
    rows: list[dict[str, Any]] = []
    for m in items:
        rel = m.source_path or f"{connector_name}/{m.item_id}"
        d = route_path(rel, rules, default_disposition=default, default_tier=default_tier)
        totals["files"] += 1
        totals["bytes"] += int(m.size or 0)
        totals[d.disposition.value] += 1
        rows.append({
            "name": m.display_name, "path": m.source_path, "bytes": m.size,
            "disposition": d.disposition.value,
            "lands_at": str(Path(bronze_root) / connector_name / subdir[d.disposition] / day),
        })
    return rows, totals


def _human_bytes(n: Any) -> str:
    try:
        size = float(n)
    except (TypeError, ValueError):
        return "size unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # pragma: no cover


__all__ = ["DRY_RUN_SHOWN", "Step", "Verification", "auth_for", "diagnose_folder", "diagnose_sign_in",
           "dry_run", "list_folders", "open_folder", "sign_in", "verify"]
