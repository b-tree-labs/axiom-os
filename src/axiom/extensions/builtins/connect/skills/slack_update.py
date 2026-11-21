# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""In-place Slack connector update (ADR-074) — evolve a deployed connector
without a teardown.

A deployed Slack app does not need to be torn down and re-onboarded to pick up
new scopes, events, or a new name. ``apps.manifest.update`` patches the app in
place (same app_id, same tokens). The only time a human is involved is when the
update *adds OAuth scopes* — then the workspace must re-consent (one "Reinstall"
click), but the ``xoxb-``/``xapp-`` tokens persist (we keep token rotation off),
so the keystore ``secret_ref`` never changes.

``classify_manifest_change`` diffs the live manifest against the desired one and
says exactly what the update costs:

- ``config_only``  — name/description/events/interactivity → API call, no human.
- ``scope_add``    — new bot scopes → API call + one re-consent click, same token.
- ``scope_removal``— only scopes dropped → API call, no re-consent needed.

This generalises across ``install_kind`` (the caller picks the updater); here we
implement the ``app_manifest`` case.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from axiom.infra.skills import SkillResult

from .slack_install import build_app_manifest


def _bot_scopes(manifest: dict) -> set[str]:
    return set(((manifest.get("oauth_config") or {}).get("scopes") or {}).get("bot") or [])


def _bot_events(manifest: dict) -> set[str]:
    return set(((manifest.get("settings") or {}).get("event_subscriptions") or {}).get("bot_events") or [])


def classify_manifest_change(current: dict, desired: dict) -> dict:
    """Diff two manifests and classify the human cost of applying ``desired``."""
    added_scopes = sorted(_bot_scopes(desired) - _bot_scopes(current))
    removed_scopes = sorted(_bot_scopes(current) - _bot_scopes(desired))
    added_events = sorted(_bot_events(desired) - _bot_events(current))
    name_changed = (
        (current.get("display_information") or {}).get("name")
        != (desired.get("display_information") or {}).get("name")
    )

    if added_scopes:
        kind = "scope_add"
        # New scopes require the workspace to re-consent — but the token persists.
        needs_reconsent = True
    elif removed_scopes:
        kind = "scope_removal"
        needs_reconsent = False
    elif added_events or name_changed:
        kind = "config_only"
        needs_reconsent = False
    else:
        kind = "noop"
        needs_reconsent = False

    return {
        "kind": kind,
        "needs_reconsent": needs_reconsent,
        "added_scopes": added_scopes,
        "removed_scopes": removed_scopes,
        "added_events": added_events,
        "name_changed": name_changed,
    }


def _default_manifest_update_api(token: str, app_id: str, manifest: dict) -> dict:  # pragma: no cover - live HTTP
    import urllib.request

    data = urllib.parse.urlencode(
        {"token": token, "app_id": app_id, "manifest": json.dumps(manifest)}
    ).encode()
    req = urllib.request.Request("https://slack.com/api/apps.manifest.update", data=data)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _default_manifest_export_api(token: str, app_id: str) -> dict:  # pragma: no cover - live HTTP
    import urllib.request

    data = urllib.parse.urlencode({"token": token, "app_id": app_id}).encode()
    req = urllib.request.Request("https://slack.com/api/apps.manifest.export", data=data)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def slack_update(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Update a deployed Slack app in place to match the current descriptor.

    Required: ``config_token`` (xoxe-), ``app_id``, and the desired ``site``
    (and optional ``channel``) used to rebuild the manifest. Inject
    ``export_api``/``update_api`` for tests.
    """
    missing = [k for k in ("config_token", "app_id") if not params.get(k)]
    if missing:
        return SkillResult(ok=False, errors=[f"missing required params: {', '.join(missing)}"])

    token = params["config_token"]
    app_id = params["app_id"]
    site = params.get("site") or "Axiom"
    channel = params.get("channel")
    desired = build_app_manifest(site=site, channel=channel)

    export_api: Callable[[str, str], dict] = params.get("export_api") or _default_manifest_export_api
    update_api: Callable[[str, str, dict], dict] = params.get("update_api") or _default_manifest_update_api

    exported = export_api(token, app_id)
    if not exported.get("ok", True):
        return SkillResult(ok=False, errors=[f"apps.manifest.export failed: {exported}"])
    current = exported.get("manifest") or {}

    change = classify_manifest_change(current, desired)
    actions = [f"diffed manifest: {change['kind']}"]

    if change["kind"] == "noop":
        return SkillResult(ok=True, value={"change": change, "applied": False,
                                           "message": "Already up to date — nothing to push."},
                           actions_taken=actions)

    result = update_api(token, app_id, desired)
    if not result.get("ok", True):
        return SkillResult(ok=False, errors=[f"apps.manifest.update failed: {result}"], actions_taken=actions)
    actions.append("applied apps.manifest.update (same app_id, tokens unchanged)")

    value: dict[str, Any] = {"change": change, "applied": True, "manifest": desired}
    if change["needs_reconsent"]:
        # Reconsent URL + reassurance copy come from the descriptor (DATA), with
        # safe fallbacks so the skill works even if the caller passes nothing.
        url_tmpl = params.get("reconsent_url") or "https://api.slack.com/apps/{app_id}/install-on-team"
        note = params.get("reconsent_note") or (
            "Your tokens stay valid — no re-paste, the keystore secret_ref is unchanged."
        )
        value["reconsent_url"] = url_tmpl.format(app_id=app_id)
        value["next_steps"] = (
            f"New scopes ({', '.join(change['added_scopes'])}) need one re-consent: open "
            f"reconsent_url and click Reinstall. {note}"
        )
    else:
        value["next_steps"] = "Live now — no human step (no new scopes, tokens unchanged)."
    return SkillResult(ok=True, value=value, actions_taken=actions)


__all__ = ["classify_manifest_change", "slack_update"]
