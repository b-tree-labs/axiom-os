# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.forward``: deliver this node's landed batches upstream (``share-upstream``).

One pass, or a loop with ``watch_s``. Reads this node's ingest outbox in order
and sends each batch to the upstream intake when it is healthy, to a Box drop
folder (through rclone, with the operator's own Box login from the vault) when
it is not, and waits otherwise. The cursor advances only on a confirmed
delivery; the upstream dedupes by content hash, so nothing lands twice.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def _secret(ref: str) -> str:
    from axiom.extensions.builtins.secrets import SecretRef, resolve

    with resolve(SecretRef.parse(ref)) as secret:
        return secret.as_str().strip()


def _hostname() -> str:
    import re
    import socket

    return re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname().split(".")[0])[:100] or "node"


def _parse_map(raw: str) -> dict[str, str]:
    out = {}
    for pair in (raw or "").replace(",", " ").split():
        if "=" in pair:
            a, b = pair.split("=", 1)
            out[a.strip()] = b.strip()
    return out


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from ..agents.plinth.connectors import load_connector
    from ..forward import BoxDropTarget, Forwarder, IntakeTarget, LocalOutbox
    from ..ingest_sink.edge import OUTBOX_DIR_ENV
    from ..sources.edge.puller import FileCursor

    outbox_dir = params.get("outbox_dir") or os.environ.get(OUTBOX_DIR_ENV, "")
    if not outbox_dir:
        return SkillResult(ok=False, errors=[f"no outbox: pass outbox_dir or set {OUTBOX_DIR_ENV} on this node"])
    state_dir = Path(params["state_dir"]) if params.get("state_dir") else ctx.state_dir
    work = Path(state_dir) / "forward"

    intake = None
    url = (params.get("intake_url") or "").strip()
    if url:
        key_ref = (params.get("key_ref") or "").strip()
        if not key_ref:
            return SkillResult(ok=False, errors=["intake_url needs key_ref (a vault reference to this site's key)"])
        try:
            token = _secret(key_ref)
        except Exception as exc:  # noqa: BLE001 - a missing key is a clear, fixable message
            return SkillResult(ok=False, errors=[f"cannot read the key {key_ref!r} from the vault: {exc}"])
        intake = IntakeTarget(url, token=token, probe_source=str(params.get("probe_source") or ""))

    box = None
    remote = (params.get("box_remote") or "").strip()
    if remote:
        login = (params.get("box_login") or "").strip()
        if login:
            from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

            vault = ForeignCredentialStore(ctx.state_dir)

            # Box refresh tokens rotate on use: the login is read from the vault
            # for each pass and a rotated one is written straight back.
            def _load(_name=login) -> str:
                with vault.get(_name) as secret:
                    return secret.as_str().strip()

            def _save(value: str, _name=login) -> None:
                vault.set(_name, value.encode("utf-8"))

            box = BoxDropTarget(remote, load_token=_load, save_token=_save)
        else:
            box = BoxDropTarget(remote)

    if intake is None and box is None:
        return SkillResult(ok=False, errors=["nothing to forward to: give intake_url and/or box_remote"])

    def bronze_root_for(source: str) -> Path:
        return Path(load_connector(source, state_dir=Path(params["state_dir"]) if params.get("state_dir") else None).bronze_root)

    policy = None
    share = params.get("share_config")
    if share:
        if isinstance(share, str):
            import tomllib

            share = tomllib.loads(Path(share).read_text()).get("share") or {}
        try:
            from ..forward_policy import TableSharePolicy
        except ImportError:
            return SkillResult(ok=False, errors=["share_config needs the sharing-policy module "
                                                 "(data_platform.forward_policy), not installed in this build"])
        policy = TableSharePolicy(dict(share))

    fwd = Forwarder(
        LocalOutbox(outbox_dir, bronze_root_for=bronze_root_for),
        cursor=FileCursor(work / "cursor.json"),
        intake=intake,
        box=box,
        policy=policy,
        source_map=_parse_map(str(params.get("source_map") or "")),
        status_path=work / "status.json",
        up_after=int(params.get("up_after") or 3),
        down_after=int(params.get("down_after") or 2),
        # After an outage: live first, the backlog paced behind it (see Forwarder).
        live_window_s=float(params.get("live_window_s") or 60),
        backlog_records_per_request=int(params.get("backlog_records_per_request") or 50),
        # The upstream sees catch-up progress and this node's disk in a heartbeat.
        beat_node=str(params.get("node") or os.environ.get("AXIOM_NODE_NAME") or _hostname()) + "-forward",
    )
    watch = float(params.get("watch_s") or 0)
    passes = int(params.get("max_passes") or (0 if watch else 1))
    total = {"sent_intake": 0, "sent_box": 0, "excluded_by_policy": 0, "held_by_policy": 0}
    n = 0
    while True:
        try:
            r = fwd.forward_once()
        except (OSError, ValueError) as exc:
            if not watch:
                return SkillResult(ok=False, errors=[f"forward stopped: {exc}; delivered batches are kept, rerun to resume"])
            # A watching forwarder is a service: one bad pass (a dropped
            # connection, a host out of ports) must not end it. Nothing was
            # confirmed, so nothing advanced; the next pass retries.
            n += 1
            if passes and n >= passes:
                return SkillResult(ok=False, errors=[f"forward stopped: {exc}; delivered batches are kept"])
            time.sleep(watch)
            continue
        for k in total:
            total[k] += getattr(r, k)
        if r.more:
            # The pass stopped at its time slice with more queued: carry on
            # without pausing. Each pass re-checks the transport first, so a
            # long drain never keeps the forwarder from a recovered intake.
            continue
        n += 1
        if (passes and n >= passes) or not watch:
            break
        time.sleep(watch)
    h = fwd.health()
    value = {**total, "current": r.current, "reason": r.reason, "after": r.after,
             "catch_up": h["catch_up"], "refusal": h["refusal"], "local_disk": h["local_disk"],
             "status_file": str(work / "status.json")}
    actions = [f"forward via {r.current}: {total['sent_intake']} to the intake, {total['sent_box']} to the drop"
               + (f" ({r.reason})" if r.reason else "")]
    cu = h["catch_up"]
    if cu.get("active"):
        eta = f", about {cu['eta_s']:.0f}s to go" if cu.get("eta_s") is not None else ""
        actions.append(f"catching up: {cu.get('remaining')} of {cu.get('backlog_total')} batches left{eta}; "
                       "live data goes first")
    if (h["local_disk"] or {}).get("alarm"):
        d = h["local_disk"]
        actions.append(f"DISK ALARM: {d['free_bytes'] // 2**20} MiB free at {d['path']} "
                       f"(alarm below {d['alarm_bytes'] // 2**20} MiB); the outbox and medallion live here")
    return SkillResult(ok=True, value=value, actions_taken=actions)
