# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``notifications.setup`` skill — configure a delivery channel durably.

Collapses the manual channel-setup runbook into one verb, generic across
every channel the rehydrator knows. The operator's job shrinks to: click
the printed link, do the one vendor-UI act (create the webhook), paste the
URL back. Everything else — validation, durable persistence, registration,
verification — is code.

Two modes:

- **links mode** (no ``webhook_url``): emit the exact clickable URLs for
  the vendor-side step, deep-linked to the right team/channel when the
  identities are stored, plus the exact next command.
- **apply mode** (``webhook_url`` given): validate the URL shape for the
  vendor, persist to the installed config (``herald.toml``, 0600 — the
  ADR-016 layer env vars override), add the channel to
  ``expected_channels`` (so ``axi doctor`` alarms if it ever silently
  unregisters), rebuild the send context, and CONFIRM the channel actually
  registered. Fail-closed: any miss is ``ok=False`` with the fix.

Storage is one file so a fresh node restores every channel with it.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any
from urllib.parse import quote, urlsplit

from axiom.infra.skills import SkillContext, SkillResult

# --- the catalog: everything channel-specific lives HERE -------------------
# key: channel name as the rehydrator registers it.
# config_key: herald.toml key / AXIOM_HERALD_ env suffix (lowercased in toml).
# host_ok: substring the webhook URL's host must contain (fail-closed shape check).
# vendor_step: the one human act, one line.
# links(identity) -> [(label, url)]: clickable, deep-linked when identity known.
_WEBHOOK_CHANNELS: dict[str, dict[str, Any]] = {
    "teams": {
        "config_key": "TEAMS_WEBHOOK_URL",
        "host_ok": ("logic.azure.com", "api.powerplatform.com"),
        "vendor_step": (
            "In the channel: ... -> Workflows -> 'Post to a channel when a "
            "webhook request is received' -> Add workflow -> copy the URL"
        ),
        "links": lambda ident: [
            (
                "open the target channel",
                "https://teams.microsoft.com/l/channel/{cid}/{cname}?groupId={gid}&tenantId={tid}".format(
                    cid=quote(ident["channel_id"], safe=""),
                    cname=quote(ident.get("channel_name", "channel"), safe=""),
                    gid=ident["group_id"],
                    tid=ident["tenant_id"],
                ),
            )
            if ident.get("channel_id") and ident.get("group_id") and ident.get("tenant_id")
            else ("open Teams", "https://teams.microsoft.com"),
            (
                "workflow template (if the in-channel picker hides it)",
                "https://make.powerautomate.com/templates?q="
                + quote("Post to a channel when a webhook request is received"),
            ),
        ],
    },
    "slack": {
        "config_key": "SLACK_WEBHOOK_URL",
        "host_ok": ("hooks.slack.com",),
        "vendor_step": (
            "Create app -> Incoming Webhooks ON -> Add New Webhook to "
            "Workspace -> pick the channel -> copy the URL"
        ),
        "links": lambda ident: [
            ("create the Slack app / webhook", "https://api.slack.com/apps?new_app=1"),
        ],
    },
    "mattermost": {
        "config_key": "MATTERMOST_WEBHOOK_URL",
        "host_ok": (),  # self-hosted: any https host
        "vendor_step": (
            "Integrations -> Incoming Webhooks -> Add -> pick the channel -> copy the URL"
        ),
        "links": lambda ident: [
            ("Mattermost integrations (your server)", "https://<your-mattermost>/integrations"),
        ],
    },
}

# Identity keys stored alongside (used only to mint deep links).
_IDENTITY_KEYS = ("tenant_id", "group_id", "channel_id", "channel_name")


def _write_installed(updates: dict[str, Any]) -> str:
    """Merge ``updates`` into herald.toml atomically (0600). Returns path."""
    import tomllib

    from axiom.extensions.builtins.notifications.channel_config import _config_path

    path = _config_path()
    doc: dict[str, Any] = {}
    try:
        with path.open("rb") as fh:
            doc = tomllib.load(fh)
    except (OSError, Exception):  # noqa: BLE001 - absent/corrupt -> rebuild
        doc = {}
    section = dict(doc.get("herald") or {})
    section.update(updates)

    lines = ["[herald]"]
    for key, value in sorted(section.items()):
        if isinstance(value, list):
            rendered = "[" + ", ".join(f'"{v}"' for v in value) + "]"
        else:
            rendered = f'"{value}"'
        lines.append(f"{key} = {rendered}")
    body = "\n".join(lines) + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".herald-")
    try:
        os.write(fd, body.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return str(path)


def _clean_pasted(raw: Any) -> str:
    """Sanitize a pasted URL: strip whitespace + any quote characters.

    Pastes arrive wrapped in straight or typographic quotes (chat renderers
    convert ' to \u2018/\u2019), and a webhook URL never legitimately
    contains any quote character — so strip them all rather than requiring
    the operator to win a shell-quoting fight.
    """
    text = str(raw or "").strip()
    for quote_char in "'\"\u2018\u2019\u201c\u201d":
        text = text.replace(quote_char, "")
    return text.strip()


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    channel = (params.get("channel") or "").strip().lower()
    spec = _WEBHOOK_CHANNELS.get(channel)
    if spec is None:
        return SkillResult(
            ok=False,
            errors=[(
                f"unknown channel {channel!r} — supported: "
                + ", ".join(sorted(_WEBHOOK_CHANNELS))
            )],
        )

    from axiom.extensions.builtins.notifications.channel_config import (
        _load_installed,
    )

    identity = {
        k: (params.get(k) or "").strip() for k in _IDENTITY_KEYS if params.get(k)
    }
    installed = _load_installed()
    # stored identities fill gaps so links regenerate on later runs
    for k in _IDENTITY_KEYS:
        identity.setdefault(k, installed.get(f"AXIOM_HERALD_TEAMS_{k.upper()}", ""))

    url = _clean_pasted(params.get("webhook_url"))
    if not url:
        if channel == "teams" and any(params.get(k) for k in _IDENTITY_KEYS):
            _write_installed(
                {f"teams_{k}": v for k, v in identity.items() if v}
            )
        return SkillResult(
            ok=True,
            value={
                "mode": "links",
                "channel": channel,
                "vendor_step": spec["vendor_step"],
                "links": [
                    {"label": label, "url": u} for label, u in spec["links"](identity)
                ],
                "next": (
                    f"axi notifications setup {channel} "
                    "--webhook-url '<paste the URL here>'"
                ),
            },
        )

    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or (
        spec["host_ok"] and not any(h in host for h in spec["host_ok"])
    ):
        return SkillResult(
            ok=False,
            errors=[(
                f"that does not look like a {channel} webhook URL "
                f"(expected https host containing {' or '.join(spec['host_ok']) or 'a hostname'}; "
                f"got {parts.scheme}://{host or '?'})"
            )],
        )

    # persist: webhook + expected_channels (+ identity for future deep links)
    expected_raw = params.get("expect") or installed.get(
        "AXIOM_HERALD_EXPECTED_CHANNELS", "inbox"
    )
    expected = {c.strip() for c in str(expected_raw).split(",") if c.strip()}
    expected.add("inbox")
    expected.add(channel)
    updates: dict[str, Any] = {
        spec["config_key"].lower(): url,
        "expected_channels": sorted(expected),
    }
    if channel == "teams":
        for k, v in identity.items():
            if v:
                updates[f"teams_{k}"] = v
    path = _write_installed(updates)

    # verify: a FRESH context must actually register the channel
    from axiom.extensions.builtins.notifications.send import SendContext

    registered = sorted(
        p.name for p in SendContext.default().registry.all()
    )
    if channel not in registered:
        return SkillResult(
            ok=False,
            errors=[(
                f"persisted to {path} but {channel!r} did not register "
                f"(registered: {', '.join(registered)}) — check the URL and "
                "any AXIOM_HERALD_* env overrides shadowing the installed config"
            )],
        )

    return SkillResult(
        ok=True,
        value={
            "mode": "applied",
            "channel": channel,
            "persisted_to": path,
            "registered": registered,
            "expected_channels": sorted(expected),
            "guarded_by": "axi doctor — 'HERALD declared channels registered'",
            "prove_it": (
                "axi notifications send --recipient <who> --priority low "
                f"--summary '{channel} channel configured' --dedup-key setup:{channel}"
            ),
        },
    )


__all__ = ["run"]
