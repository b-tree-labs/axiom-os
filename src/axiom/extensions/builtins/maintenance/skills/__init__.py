# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The ``maintenance`` and ``support`` capabilities (ADR-056, ADR-183).

On a site's node:

* ``maintenance.status`` and ``maintenance.pending`` are read-only;
* ``maintenance.poll`` is what the node's maintenance service runs: fetch the
  requests addressed to this site from the relay, act on each, post results;
* ``maintenance.approve`` and ``maintenance.deny`` are a person at the site
  deciding on a waiting change;
* ``support.open`` and ``support.close`` are a person at the site starting and
  ending a support session.

On an operator's machine: ``maintenance.keygen``, ``maintenance.request``,
``maintenance.results``, ``support.list`` and ``support.attach``.

Every one that decides or signs anything is offered at the command line only,
never to an AI client: approving a change, opening a session and signing a
request are each a person's act.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from axiom.infra import maintenance as m
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec, default_registry

RELAY_ENV = "AXIOM_MAINTENANCE_RELAY"
TOKEN_REF_ENV = "AXIOM_MAINTENANCE_TOKEN_REF"
SIGNING_KEY_ENV = "AXIOM_MAINTENANCE_SIGNING_KEY"
KEY_ID_ENV = "AXIOM_MAINTENANCE_KEY_ID"
OPERATOR_ENV = "AXIOM_MAINTENANCE_OPERATOR"


def _secret(ref: str) -> str:
    """A credential by vault name (what ``secrets set`` stores) or by ``scheme://`` reference."""
    if "://" not in ref:
        from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
        from axiom.infra.paths import get_user_state_dir

        with ForeignCredentialStore(get_user_state_dir()).get(ref) as secret:
            return secret.as_str().strip()
    from axiom.extensions.builtins.secrets import SecretRef, resolve

    with resolve(SecretRef.parse(ref)) as secret:
        return secret.as_str().strip()


def _node(ctx: SkillContext) -> m.MaintenanceNode:
    return m.node_from_config(state_dir=Path(ctx.state_dir) / "maintenance")


def _node_relay(node: m.MaintenanceNode) -> tuple[str, str]:
    url = str(node.wiring.get("relay_url") or "")
    ref = str(node.wiring.get("key_ref") or "")
    if not url or not ref:
        raise ValueError(f"{m.WIRING_FILE} needs relay_url and key_ref (a vault reference to this site's key)")
    return url, _secret(ref)


def _audit(node: m.MaintenanceNode):
    def write(event: str, **extra: Any) -> None:
        node._audit(event, None, **extra)
    return write


def status(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from axiom.infra import node_functions as nf

    level = nf.load().maintenance_level
    try:
        node = _node(ctx)
    except ValueError as exc:
        return SkillResult(value={"level": level, "configured": False, "problem": str(exc)})
    wiring = node.wiring
    commands = wiring.get("commands") or {}
    wired = sorted(
        f"{a}:{t}" if isinstance(c, dict) else a
        for a, c in commands.items() for t in (c if isinstance(c, dict) else [None])
    )
    return SkillResult(value={
        **node.heartbeat_section(),
        "configured": True,
        "site": node.site,
        "relay_url": wiring.get("relay_url", ""),
        "trusted_keys": sorted((wiring.get("trusted_keys") or {}).keys()),
        "wired": wired,
        "audit_log": str(node.audit_path),
    })


def pending(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    try:
        node = _node(ctx)
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    rows = [{**{k: p[k] for k in ("id", "action", "params", "operator", "expires_at")},
             "risk": m.ACTIONS[p["action"]].risk} for p in node.pending()]
    return SkillResult(value={"pending": rows})


def poll(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    import time

    try:
        node = _node(ctx)
        url, token = _node_relay(node)
    except (ValueError, KeyError) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    client = m.EdgeMailboxClient(url, token)
    watch_s = float(params.get("watch_s") or 0)
    passes = []
    while True:
        try:
            passes.append(m.poll_once(node, client))
        except OSError as exc:
            passes.append({"error": f"relay unreachable: {exc}"})
        # The release channel is checked from this same scheduled pass (ADR-179 §3).
        from axiom.extensions.builtins.update.channel import channel_tick

        checked = channel_tick(node.wiring, node.state_dir)
        if checked is not None:
            passes[-1] = {**passes[-1], "update": checked}
        if not watch_s:
            break
        time.sleep(watch_s)
    return SkillResult(value={"passes": passes[-1:], "level": node.level})


def apply_update(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Install an approved version from the signed release channel (what ``apply_update`` is wired to)."""
    from axiom.extensions.builtins.update.channel import apply_approved

    version = str(params.get("version") or "").strip()
    if not version:
        return SkillResult(ok=False, errors=["--version is required"])
    try:
        node = _node(ctx)
    except (ValueError, KeyError) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    out = apply_approved(node.wiring, node.state_dir, version)
    ok = out.get("status") == "updated"
    return SkillResult(ok=ok, value=out, errors=[] if ok else [f"{out.get('status')}: {out.get('detail', '')}"])


def _decide(params: dict[str, Any], ctx: SkillContext, approve: bool) -> SkillResult:
    rid = str(params.get("id") or "").strip()
    by = str(params.get("by") or os.environ.get("USER") or "a person at the site")
    try:
        node = _node(ctx)
        result = node.approve(rid, by=by) if approve else node.deny(rid, by=by)
    except (ValueError, KeyError) as exc:
        return SkillResult(ok=False, errors=[str(exc).strip("'\"")])
    try:
        url, token = _node_relay(node)
        m.report(node, m.EdgeMailboxClient(url, token), result)
    except (ValueError, KeyError):
        pass  # the next poll pass posts it
    verb = "approved" if approve else "denied"
    return SkillResult(value=result, actions_taken=[f"{verb} {result.get('action')} ({rid}): {result['status']}"])


def approve(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    return _decide(params, ctx, True)


def deny(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    return _decide(params, ctx, False)


# -- operator side -------------------------------------------------------------


def _vault(ctx: SkillContext):
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

    return ForeignCredentialStore(Path(ctx.state_dir))


def keygen(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Make an operator signing key; the private half goes straight into the vault."""
    from axiom.vega.identity.keypair import generate_keypair

    name = str(params.get("name") or "").strip()
    if not name:
        return SkillResult(ok=False, errors=["name the key (it becomes its key_id on the nodes that trust it)"])
    kp = generate_keypair()
    _vault(ctx).set(f"maintenance-signing-{name}", m.b64(kp.export_private()).encode(),
                    notes="remote maintenance operator signing key (ADR-183)")
    return SkillResult(
        value={"key_id": name, "public_key": m.b64(kp.public_bytes)},
        actions_taken=[f"stored the private key in the vault as maintenance-signing-{name}"],
    )


def _operator_conf(params: dict[str, Any]) -> dict[str, str]:
    conf = {
        "relay_url": params.get("relay_url") or os.environ.get(RELAY_ENV, ""),
        "token_ref": params.get("token_ref") or os.environ.get(TOKEN_REF_ENV, ""),
        "key_id": params.get("key_id") or os.environ.get(KEY_ID_ENV, ""),
        "operator": params.get("operator") or os.environ.get(OPERATOR_ENV, ""),
    }
    missing = [k for k in ("relay_url", "token_ref") if not conf[k]]
    if missing:
        raise ValueError(f"missing {', '.join(missing)} (flags, or {RELAY_ENV} / {TOKEN_REF_ENV})")
    return {k: str(v) for k, v in conf.items()}


def _post(conf: dict[str, str], method: str, path: str, body: Any = None) -> Any:
    import json
    import urllib.request

    token = _secret(conf["token_ref"])
    req = urllib.request.Request(conf["relay_url"].rstrip("/") + "/maintenance" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - the configured relay
        return json.loads(resp.read() or b"{}")


def request(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Sign one maintenance request for a site and hand it to the relay."""
    from axiom.vega.identity.keypair import Keypair

    try:
        conf = _operator_conf(params)
        if not conf["key_id"] or not conf["operator"]:
            raise ValueError(f"missing key_id or operator ({KEY_ID_ENV} / {OPERATOR_ENV})")
        site, action = str(params["site"]), str(params["action"])
        if action not in m.ACTIONS:
            raise ValueError(f"{action!r} is not an allowlisted action: {', '.join(m.ACTIONS)}")
        with _vault(ctx).get(f"maintenance-signing-{conf['key_id']}") as secret:
            kp = Keypair.from_private_bytes(m.unb64(secret.as_str().strip()))
    except (KeyError, ValueError) as exc:
        return SkillResult(ok=False, errors=[str(exc).strip("'\"")])
    now = datetime.now(UTC)
    ttl = int(params.get("ttl_s") or 3600)
    envelope = m.sign_request(kp, key_id=conf["key_id"], operator_principal=conf["operator"], site=site,
                              action=action, params=dict(params.get("params") or {}),
                              issued_at=now, expires_at=now + timedelta(seconds=ttl))
    try:
        out = _post(conf, "POST", "/requests", envelope)
    except OSError as exc:
        return SkillResult(ok=False, errors=[f"the relay refused or was unreachable: {exc}"])
    return SkillResult(value={"id": envelope["request"]["id"], "seq": out.get("seq"), "site": site,
                              "action": action, "expires_at": envelope["request"]["expires_at"]},
                       actions_taken=[f"sent {action} for {site}"])


def results(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    try:
        conf = _operator_conf(params)
        out = _post(conf, "GET", f"/results?site={params['site']}&after={int(params.get('after') or 0)}")
    except (KeyError, ValueError, OSError) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(value=out)


# -- support sessions ------------------------------------------------------------


def support_open(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    import sys

    from axiom.infra import node_functions as nf
    from axiom.infra.support_session import SupportClient, parse_duration, run_session

    level = nf.load().maintenance_level
    if level != "sessions":
        return SkillResult(ok=False, errors=[
            f'this site declared maintenance = "{level}"; support sessions need "sessions" '
            "(`features maintenance sessions`), a site decision"])
    try:
        duration = parse_duration(str(params.get("for") or ""))
        node = _node(ctx)
        url, token = _node_relay(node)
    except (ValueError, KeyError) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    shell = list(node.wiring.get("support_shell") or [os.environ.get("SHELL") or "/bin/sh", "-i"])
    mirror = None
    if params.get("mirror", True):
        def mirror(data: bytes) -> None:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    def on_open(sess: dict[str, Any]) -> None:
        print(f"Support session {sess['id']} is open until "
              f"{datetime.fromtimestamp(sess['expires_at'], UTC):%H:%M} UTC. Everything typed and printed "
              "shows here and is recorded. Ctrl-C here, or `support close` anywhere on this machine, ends it.",
              file=sys.stderr, flush=True)

    out = run_session(SupportClient(url, token), duration_s=duration, reason=str(params.get("reason") or ""),
                      shell=shell, state_dir=node.state_dir, node=node.site, mirror=mirror,
                      on_open=on_open, audit=_audit(node))
    return SkillResult(value=out, actions_taken=[f"support session {out['id']} ended: {out['ended']}"])


def support_close(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from axiom.infra.support_session import SupportClient, close_active

    try:
        node = _node(ctx)
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    try:
        url, token = _node_relay(node)
        client = SupportClient(url, token)
    except (ValueError, KeyError):
        client = None
    active = close_active(client, node.state_dir)
    if active is None:
        return SkillResult(value={"closed": None}, actions_taken=["no support session is open on this machine"])
    return SkillResult(value={"closed": active["id"]}, actions_taken=[f"ended support session {active['id']}"])


def _operator_client(params: dict[str, Any]):
    from axiom.infra.support_session import SupportClient

    conf = _operator_conf(params)
    return SupportClient(conf["relay_url"], _secret(conf["token_ref"]))


def support_list(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    try:
        return SkillResult(value={"sessions": _operator_client(params).list(str(params["site"]))})
    except (KeyError, ValueError, OSError) as exc:
        return SkillResult(ok=False, errors=[str(exc)])


def support_attach(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from axiom.infra.support_session import attach

    try:
        client = _operator_client(params)
        why = attach(client, str(params["id"]))
    except (KeyError, ValueError, OSError) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(value={"id": params["id"], "ended": why})


def _spec(name, fn, description, inputs, *, side_effects, surfaces=("cli", "skill_md"), idempotent=False):
    return SkillSpec(name=name, fn=fn, description=description, inputs=inputs, side_effects=side_effects,
                     idempotent=idempotent, surfaces=surfaces)


_READ = ("cli", "mcp", "agent_tool", "skill_md")
_SPECS = (
    _spec("maintenance.status", status, "This node's remote-maintenance level, what is waiting for approval, "
          "what ran recently, and what the deployment wired. Read-only.", {}, side_effects=False,
          surfaces=_READ, idempotent=True),
    _spec("maintenance.pending", pending, "Maintenance requests waiting for a person at this site to approve "
          "or deny. Read-only.", {}, side_effects=False, surfaces=_READ, idempotent=True),
    _spec("maintenance.poll", poll, "Fetch this site's maintenance requests from the relay over the outbound "
          "connection, act on each, and post the results. watch_s repeats.", {"watch_s": "float?"},
          side_effects=True),
    _spec("maintenance.apply_update", apply_update, "Install a version a person approved, from the signed "
          "release channel, with checks and rollback. What a node wires apply_update to.",
          {"version": "str"}, side_effects=True),
    _spec("maintenance.approve", approve, "Approve a waiting maintenance request; it runs now.",
          {"id": "str", "by": "str?"}, side_effects=True),
    _spec("maintenance.deny", deny, "Deny a waiting maintenance request; it never runs.",
          {"id": "str", "by": "str?"}, side_effects=True),
    _spec("maintenance.keygen", keygen, "Make an operator signing key; the private half goes into the vault.",
          {"name": "str"}, side_effects=True),
    _spec("maintenance.request", request, "Sign one allowlisted maintenance request for a site and send it "
          "to the relay.", {"site": "str", "action": "str", "params": "dict?", "ttl_s": "int?",
                            "relay_url": "str?", "token_ref": "str?", "key_id": "str?", "operator": "str?"},
          side_effects=True),
    _spec("maintenance.results", results, "Results a site's node has reported for maintenance requests.",
          {"site": "str", "after": "int?", "relay_url": "str?", "token_ref": "str?"}, side_effects=False,
          idempotent=True),
    _spec("support.open", support_open, "Open a time-boxed, recorded support session from this machine. "
          "Run by a person at the site, who can end it.", {"for": "str", "reason": "str?"}, side_effects=True),
    _spec("support.close", support_close, "End the support session open on this machine.", {},
          side_effects=True, idempotent=True),
    _spec("support.list", support_list, "Support sessions a site has open at the relay.",
          {"site": "str", "relay_url": "str?", "token_ref": "str?"}, side_effects=False, idempotent=True),
    _spec("support.attach", support_attach, "Join an open support session as the operator (Ctrl-] leaves).",
          {"id": "str", "relay_url": "str?", "token_ref": "str?"}, side_effects=True),
)


def bind(registry: SkillRegistry) -> list[str]:
    names = []
    for spec in _SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=spec.side_effects)
        names.append(spec.name)
    return names


register = bind


def bind_default() -> SkillRegistry:
    registry = default_registry()
    bind(registry)
    return registry


__all__ = [
    "approve", "bind", "bind_default", "deny", "keygen", "pending", "poll", "register", "request",
    "results", "status", "support_attach", "support_close", "support_list", "support_open",
]
