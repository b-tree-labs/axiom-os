# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Remote maintenance of a site's node, without anyone logging in (ADR-183).

A site declares how much remote maintenance it accepts, the way it declares its
agent policy (``node.toml``, ``[node] maintenance = ...``):

* ``off``       nothing remote runs here. The node still reports its health.
* ``requests``  (level 1) an operator may send a signed request for one action
                from a fixed allowlist. Read-only actions run; anything that
                changes the machine waits for a person at the site to approve it.
* ``sessions``  (level 2) as ``requests``, and a person at the site may open a
                time-boxed support session (``support open``). Only they can
                open one, and they can end it.

Every connection is outbound from the node: it polls the relay it already sends
data to for requests addressed to its site, and posts the results back. No port
is opened and no account exists on the machine for the operator.

The node is the authority, not the relay. A request runs only if it is signed by
an operator key this node trusts, addressed to this site, inside its validity
window, never seen before, an allowlisted action with valid parameters, wired to
a command on this node by whoever deployed it, and approved where approval is
required. Commands are fixed argument lists with parameters substituted as whole
arguments, never a shell. Every decision is appended to the node's audit log.

Two files, two owners:

* the site's policy, in ``node.toml`` (``maintenance`` and the approval rule),
  is a site decision;
* the wiring, ``maintenance.toml`` (trusted operator keys and the command each
  action runs), comes with the deployment (the role's infrastructure-as-code
  definition), so what an action does on this machine is reviewable in one place.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from axiom.vega.identity.keypair import Keypair, verify

LEVELS: tuple[str, ...] = ("off", "requests", "sessions")
#: Which actions wait for a person at the site: changes only (the default), all, or none.
APPROVAL_RULES: tuple[str, ...] = ("change", "all", "none")

#: A request may be valid for at most this long, so a leaked one goes stale.
MAX_VALIDITY = timedelta(hours=24)
#: Clock skew tolerated between the operator and the node.
SKEW = timedelta(minutes=5)
#: How much of a command's output comes back.
OUTPUT_TAIL = 16_000
DEFAULT_TIMEOUT_S = 900

_SLUG = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
_VERSION = r"\d+(\.\d+){1,3}([a-z0-9.+-]{0,20})?"
_ISO = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})"
_SETTING_VALUE = r"[A-Za-z0-9 ._:/@+=,-]{0,256}"


@dataclass(frozen=True)
class ActionSpec:
    name: str
    #: "read" never changes the machine; "change" does, and waits for approval.
    risk: str
    #: parameter name -> regular expression its value must fully match.
    params: Mapping[str, str] = field(default_factory=dict)
    #: For actions wired per target (``restart_service`` per service), the
    #: parameter that selects which command runs.
    selector: str = ""
    description: str = ""


#: The only actions a request can name. Adding one is a reviewed code change.
ACTIONS: dict[str, ActionSpec] = {
    a.name: a
    for a in (
        ActionSpec("diagnose", "read", description="run the node's own diagnosis and report it"),
        ActionSpec("support_bundle", "read", description="collect a support bundle (no secrets) and report it"),
        ActionSpec("restart_service", "change", {"service": _SLUG}, selector="service",
                   description="restart one of the services this node declares"),
        ActionSpec("apply_update", "change", {"version": _VERSION},
                   description="update to a specific released version, with the node's own self-check and rollback"),
        ActionSpec("set_setting", "change", {"key": _SLUG, "value": _SETTING_VALUE}, selector="key",
                   description="change one setting the deployment allowlists"),
        ActionSpec("rerun_backfill", "change", {"source": _SLUG, "start_at": _ISO, "end_at": _ISO},
                   description="re-read history from the site's archive for a bounded window"),
    )
}

WIRING_FILE = "maintenance.toml"
WIRING_ENV = "AXIOM_MAINTENANCE_WIRING"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def canonical(request: Mapping[str, Any]) -> bytes:
    """The exact bytes that are signed: sorted keys, no whitespace."""
    return json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def sign_request(
    keypair: Keypair,
    *,
    key_id: str,
    operator_principal: str,
    site: str,
    action: str,
    params: Mapping[str, Any] | None = None,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """An operator's signed request envelope, ready to hand to a relay."""
    issued_at = issued_at or datetime.now(UTC)
    expires_at = expires_at or issued_at + timedelta(hours=1)
    request = {
        "id": request_id or str(uuid.uuid4()),
        "site": site,
        "action": action,
        "params": {str(k): str(v) for k, v in (params or {}).items()},
        "issued_at": _iso(issued_at),
        "expires_at": _iso(expires_at),
        "operator": operator_principal,
        "key_id": key_id,
    }
    return {"request": request, "signature": b64(keypair.sign(canonical(request)))}


def request_digest(request: Mapping[str, Any]) -> str:
    """What an approval link is bound to: the exact request, so an altered one is not approved."""
    return hashlib.sha256(canonical(request)).hexdigest()


#: The decisions a link can carry.
DECISIONS = ("approve", "deny")


def sign_approval(
    keypair: Keypair,
    *,
    key_id: str,
    operator_principal: str,
    request_envelope: Mapping[str, Any],
    recipient: str,
    decision: str = "approve",
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    """A signed one-time decision on one pending request, for one named person.

    Bound to the request's id, site and exact content (its digest), to the
    recipient it was issued to, and to an expiry no later than the request's
    own. Usable once: its ``id`` is a nonce the node remembers.
    """
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    request = dict(request_envelope["request"])
    issued_at = issued_at or datetime.now(UTC)
    req_expires = _parse(str(request["expires_at"]))
    expires_at = min(expires_at or req_expires, req_expires)
    approval = {
        "kind": "approval",
        "id": uuid.uuid4().hex,
        "request_id": str(request["id"]),
        "site": str(request["site"]),
        "request_digest": request_digest(request),
        "recipient": recipient,
        "decision": decision,
        "operator": operator_principal,
        "key_id": key_id,
        "issued_at": _iso(issued_at),
        "expires_at": _iso(expires_at),
    }
    return {"approval": approval, "signature": b64(keypair.sign(canonical(approval)))}


def link_token(envelope: Mapping[str, Any]) -> str:
    """The URL-safe form of an approval envelope, for the link's path."""
    return base64.urlsafe_b64encode(canonical(envelope)).decode().rstrip("=")


def read_link_token(token: str) -> dict[str, Any]:
    """The approval envelope inside a link token; ``ValueError`` if it is not one."""
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        envelope = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("not an approval link") from exc
    if not isinstance(envelope, dict) or not isinstance(envelope.get("approval"), dict):
        raise ValueError("not an approval link")
    return envelope


def approval_link(base_url: str, envelope: Mapping[str, Any]) -> str:
    return f"{base_url.rstrip('/')}/approve/{link_token(envelope)}"


class Refused(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


def load_wiring(path: Path | None = None) -> dict[str, Any]:
    """The deployment's wiring: trusted keys, approval rule override, commands."""
    path = path or Path(os.environ.get(WIRING_ENV) or _default_state_dir() / WIRING_FILE)
    if not path.is_file():
        return {}
    import tomllib

    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path} could not be read: {exc}") from exc


def _default_state_dir() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir()


class MaintenanceNode:
    """The node side: verify, decide, run, record."""

    def __init__(
        self,
        *,
        site: str,
        level: str,
        wiring: Mapping[str, Any],
        state_dir: Path,
        approve: str | None = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        if level not in LEVELS:
            raise ValueError(f"maintenance level must be one of {', '.join(LEVELS)}, not {level!r}")
        self.site = site
        self.level = level
        self.wiring = dict(wiring)
        self.approve_rule = approve or str(self.wiring.get("approve") or "change")
        if self.approve_rule not in APPROVAL_RULES:
            raise ValueError(f"approval rule must be one of {', '.join(APPROVAL_RULES)}")
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_s = timeout_s

    # -- files ------------------------------------------------------------
    @property
    def audit_path(self) -> Path:
        return self.state_dir / "audit.jsonl"

    @property
    def _seen_path(self) -> Path:
        return self.state_dir / "seen.json"

    @property
    def _pending_path(self) -> Path:
        return self.state_dir / "pending.json"

    @property
    def _results_path(self) -> Path:
        return self.state_dir / "results.json"

    def _read(self, path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _write(self, path: Path, data: Any) -> None:
        from axiom.infra.state import atomic_write

        atomic_write(path, data)

    def _audit(self, event: str, request: Mapping[str, Any] | None, **extra: Any) -> None:
        from axiom.infra.state import locked_append_jsonl

        rec = {"at": _iso(datetime.now(UTC)), "event": event, "site": self.site}
        if request:
            rec.update({k: request.get(k) for k in ("id", "action", "params", "operator", "key_id")})
        rec.update(extra)
        locked_append_jsonl(self.audit_path, rec)

    def _remember_result(self, result: dict[str, Any]) -> dict[str, Any]:
        results = self._read(self._results_path, [])
        results = [r for r in results if r.get("id") != result.get("id")] + [result]
        self._write(self._results_path, results[-50:])
        return result

    # -- verification -----------------------------------------------------
    def verify(self, envelope: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        """The request inside ``envelope`` if this node should accept it; else :class:`Refused`."""
        now = now or datetime.now(UTC)
        if self.level == "off":
            raise Refused("maintenance_off", "this site has turned remote maintenance off")
        request = envelope.get("request")
        if not isinstance(request, Mapping):
            raise Refused("malformed", "no request in the envelope")
        request = dict(request)
        keys = self.wiring.get("trusted_keys") or {}
        key = keys.get(str(request.get("key_id") or ""))
        if not key:
            raise Refused("unknown_key", "signed with a key this node does not trust")
        try:
            ok = verify(unb64(str(key["public_key"])), canonical(request), unb64(str(envelope.get("signature") or "")))
        except (ValueError, KeyError):
            ok = False
        if not ok:
            raise Refused("bad_signature", "the signature does not match the request")
        if key.get("operator") and key["operator"] != request.get("operator"):
            raise Refused("bad_signature", "the key does not belong to the operator the request names")
        if request.get("site") != self.site:
            raise Refused("wrong_site", f"addressed to {request.get('site')!r}, this node is {self.site!r}")
        try:
            issued, expires = _parse(str(request["issued_at"])), _parse(str(request["expires_at"]))
        except (KeyError, ValueError) as exc:
            raise Refused("malformed", f"bad validity window: {exc}") from exc
        if expires - issued > MAX_VALIDITY:
            raise Refused("validity_too_long", f"a request may be valid for at most {MAX_VALIDITY}")
        if issued - SKEW > now:
            raise Refused("not_yet_valid", "issued in the future (check the clocks)")
        if now > expires + SKEW:
            raise Refused("expired", "this request expired before it arrived")
        spec = ACTIONS.get(str(request.get("action") or ""))
        if spec is None:
            raise Refused("unknown_action", f"{request.get('action')!r} is not an allowlisted action")
        params = request.get("params") or {}
        if set(params) != set(spec.params):
            raise Refused("bad_params", f"{spec.name} takes exactly {sorted(spec.params) or 'no parameters'}")
        for name, pattern in spec.params.items():
            if not re.fullmatch(pattern, str(params[name])):
                raise Refused("bad_params", f"{name!r} is not a valid value for {spec.name}")
        self._argv(spec, params)  # wired here, or refused before anything is recorded as accepted
        seen = self._read(self._seen_path, {})
        if str(request["id"]) in seen:
            raise Refused("replayed", "this request has already been received")
        return request

    def _argv(self, spec: ActionSpec, params: Mapping[str, str]) -> list[str]:
        wired = (self.wiring.get("commands") or {}).get(spec.name)
        if spec.selector:
            wired = (wired or {}).get(params.get(spec.selector, "")) if isinstance(wired, Mapping) else None
        if not isinstance(wired, list) or not wired:
            raise Refused("not_wired", f"this node does not wire {spec.name}"
                          + (f" for {params.get(spec.selector)!r}" if spec.selector else ""))
        out = []
        for token in wired:
            token = str(token)
            m = re.fullmatch(r"\{([a-z_]+)\}", token)
            out.append(str(params[m.group(1)]) if m and m.group(1) in params else token)
        return out

    def _mark_seen(self, request: Mapping[str, Any]) -> None:
        seen = self._read(self._seen_path, {})
        now = datetime.now(UTC)
        seen = {k: v for k, v in seen.items() if _parse(v) + MAX_VALIDITY + SKEW > now}
        seen[str(request["id"])] = str(request["expires_at"])
        self._write(self._seen_path, seen)

    # -- decision ---------------------------------------------------------
    def _needs_approval(self, spec: ActionSpec) -> bool:
        return self.approve_rule == "all" or (self.approve_rule == "change" and spec.risk == "change")

    def receive(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """Verify one envelope and act on it. Never raises for a bad request."""
        try:
            request = self.verify(envelope)
        except Refused as exc:
            req = envelope.get("request") if isinstance(envelope.get("request"), Mapping) else {}
            self._audit("refused", req, reason=exc.reason, detail=exc.detail)
            return self._remember_result({"id": (req or {}).get("id"), "action": (req or {}).get("action"),
                                          "status": "refused", "reason": exc.reason, "detail": exc.detail})
        self._mark_seen(request)
        self._audit("received", request)
        spec = ACTIONS[request["action"]]
        if self._needs_approval(spec):
            pending = self._read(self._pending_path, {})
            pending[request["id"]] = request
            self._write(self._pending_path, pending)
            self._audit("pending", request, waiting_for="a person at the site")
            return self._remember_result({"id": request["id"], "action": request["action"], "status": "pending"})
        return self._run(request)

    @property
    def _used_links_path(self) -> Path:
        return self.state_dir / "used-links.json"

    def verify_approval(self, envelope: Mapping[str, Any], *, now: datetime | None = None) -> tuple[dict, dict]:
        """``(approval, pending request)`` if this link may decide; else :class:`Refused`.

        The node is the authority, as for requests: the relay only carries the
        link. Signature by a trusted key, this site, inside its window, not used
        before, naming a request that is still pending here, and bound to that
        request's exact content.
        """
        now = now or datetime.now(UTC)
        if self.level == "off":
            raise Refused("maintenance_off", "this site has turned remote maintenance off")
        approval = envelope.get("approval")
        if not isinstance(approval, Mapping) or approval.get("kind") != "approval":
            raise Refused("malformed", "no approval in the envelope")
        approval = dict(approval)
        key = (self.wiring.get("trusted_keys") or {}).get(str(approval.get("key_id") or ""))
        if not key:
            raise Refused("unknown_key", "signed with a key this node does not trust")
        try:
            ok = verify(unb64(str(key["public_key"])), canonical(approval), unb64(str(envelope.get("signature") or "")))
        except (ValueError, KeyError):
            ok = False
        if not ok:
            raise Refused("bad_signature", "the signature does not match the link")
        if key.get("operator") and key["operator"] != approval.get("operator"):
            raise Refused("bad_signature", "the key does not belong to the operator the link names")
        if approval.get("site") != self.site:
            raise Refused("wrong_site", f"issued for {approval.get('site')!r}, this node is {self.site!r}")
        if approval.get("decision") not in DECISIONS:
            raise Refused("malformed", "the link carries no decision")
        try:
            issued, expires = _parse(str(approval["issued_at"])), _parse(str(approval["expires_at"]))
        except (KeyError, ValueError) as exc:
            raise Refused("malformed", f"bad validity window: {exc}") from exc
        if expires - issued > MAX_VALIDITY:
            raise Refused("validity_too_long", f"a link may be valid for at most {MAX_VALIDITY}")
        if issued - SKEW > now:
            raise Refused("not_yet_valid", "issued in the future (check the clocks)")
        if now > expires + SKEW:
            raise Refused("expired", "this link has expired")
        approvers = self.wiring.get("approvers")
        if approvers is not None and approval.get("recipient") not in approvers:
            raise Refused("not_an_approver", f"{approval.get('recipient')!r} may not decide for this site")
        if str(approval.get("id") or "") in self._read(self._used_links_path, {}):
            raise Refused("replayed", "this link has already been used")
        request = self._read(self._pending_path, {}).get(str(approval.get("request_id") or ""))
        if request is None:
            raise Refused("not_pending", "no such request is waiting for a decision here")
        if request_digest(request) != approval.get("request_digest"):
            raise Refused("wrong_request", "the link was issued for a different request")
        return approval, request

    def redeem(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """Act on an approval link the relay delivered. Never raises for a bad link."""
        raw = envelope.get("approval") if isinstance(envelope.get("approval"), Mapping) else {}
        try:
            approval, _request = self.verify_approval(envelope)
        except Refused as exc:
            self._audit("link_refused", None, link=raw.get("id"), request_id=raw.get("request_id"),
                        recipient=raw.get("recipient"), reason=exc.reason, detail=exc.detail)
            return self._remember_result({"id": raw.get("request_id"), "link": raw.get("id"),
                                          "status": "link_refused", "reason": exc.reason, "detail": exc.detail})
        used = self._read(self._used_links_path, {})
        now = datetime.now(UTC)
        used = {k: v for k, v in used.items() if _parse(v) + MAX_VALIDITY + SKEW > now}
        used[str(approval["id"])] = str(approval["expires_at"])
        self._write(self._used_links_path, used)
        by = str(approval["recipient"])
        self._audit("link_used", None, link=approval["id"], request_id=approval["request_id"],
                    recipient=by, decision=approval["decision"])
        if approval["decision"] == "deny":
            return self.deny(str(approval["request_id"]), by=by)
        return self.approve(str(approval["request_id"]), by=by)

    def pending(self) -> list[dict[str, Any]]:
        return list(self._read(self._pending_path, {}).values())

    def _take_pending(self, request_id: str) -> dict[str, Any]:
        pending = self._read(self._pending_path, {})
        if request_id not in pending:
            raise KeyError(f"no request {request_id!r} is waiting for approval")
        request = pending.pop(request_id)
        self._write(self._pending_path, pending)
        return request

    def approve(self, request_id: str, *, by: str) -> dict[str, Any]:
        request = self._take_pending(request_id)
        if datetime.now(UTC) > _parse(request["expires_at"]) + SKEW:
            self._audit("expired", request)
            return self._remember_result({"id": request_id, "action": request["action"], "status": "expired"})
        self._audit("approved", request, by=by)
        return self._run(request)

    def deny(self, request_id: str, *, by: str) -> dict[str, Any]:
        request = self._take_pending(request_id)
        self._audit("denied", request, by=by)
        return self._remember_result({"id": request_id, "action": request["action"], "status": "denied", "by": by})

    def sweep(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Expire requests nobody approved in time."""
        now = now or datetime.now(UTC)
        pending = self._read(self._pending_path, {})
        out = []
        for rid, request in list(pending.items()):
            if now > _parse(request["expires_at"]) + SKEW:
                pending.pop(rid)
                self._audit("expired", request)
                out.append(self._remember_result({"id": rid, "action": request["action"], "status": "expired"}))
        if out:
            self._write(self._pending_path, pending)
        return out

    # -- execution --------------------------------------------------------
    def _run(self, request: Mapping[str, Any]) -> dict[str, Any]:
        spec = ACTIONS[request["action"]]
        argv = self._argv(spec, request.get("params") or {})
        started = datetime.now(UTC)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout_s, shell=False)
            code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            code, output = -1, f"timed out after {self.timeout_s}s\n{exc.stdout or ''}"
        except OSError as exc:
            code, output = -1, f"could not start: {exc}"
        status = "done" if code == 0 else "failed"
        result = {
            "id": request["id"], "action": request["action"], "status": status, "exit_code": code,
            "started_at": _iso(started), "finished_at": _iso(datetime.now(UTC)),
            "output_tail": output[-OUTPUT_TAIL:],
        }
        self._audit(status, request, exit_code=code)
        return self._remember_result(result)

    def heartbeat_section(self) -> dict[str, Any]:
        """What the node's heartbeat says about remote maintenance."""
        results = self._read(self._results_path, [])
        section = {
            "level": self.level,
            "approve": self.approve_rule,
            "pending": len(self.pending()),
            "recent": [{k: r.get(k) for k in ("id", "action", "status", "reason", "exit_code", "finished_at")}
                       for r in results[-10:]],
        }
        # What the node's last release-channel check found, so a release
        # waiting for approval is visible to the platform (ADR-179 §6).
        from axiom.extensions.builtins.update.channel import last_check

        update = last_check(self.state_dir)
        if update:
            section["update"] = {k: update.get(k) for k in
                                 ("status", "current", "version", "notes", "from", "to", "reason", "detail", "checked_at")
                                 if update.get(k) is not None}
        return section


def node_from_config(*, node_config: Path | None = None, wiring_path: Path | None = None,
                     state_dir: Path | None = None) -> MaintenanceNode:
    """This node as the site declared it (``node.toml``) and the deployment wired it."""
    from axiom.infra import node_functions as nf

    cfg = nf.load(node_config)
    wiring = load_wiring(wiring_path)
    site = str(wiring.get("site") or cfg.settings.get("site") or "")
    if not site:
        raise ValueError(f"{WIRING_FILE} does not say which site this node is (site = ...)")
    return MaintenanceNode(
        site=site,
        level=cfg.maintenance_level,
        wiring=wiring,
        state_dir=state_dir or _default_state_dir() / "maintenance",
    )


def heartbeat_section() -> dict[str, Any] | None:
    """The maintenance part of this node's heartbeat, or None where nothing is set up.

    Never raises: a heartbeat must go out whatever state this is in.
    """
    try:
        node = node_from_config()
    except Exception:  # noqa: BLE001
        return None
    return node.heartbeat_section()


class EdgeMailboxClient:
    """The node's outbound side: fetch requests for its site, post results back."""

    def __init__(self, base_url: str, token: str, *, timeout_s: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        import urllib.request

        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, method=method, data=data, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310 - configured relay
            return json.loads(resp.read() or b"{}")

    def fetch(self, after: int) -> dict[str, Any]:
        return self._call("GET", f"/maintenance/requests?after={int(after)}")

    def post_results(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        return self._call("POST", "/maintenance/results", {"results": results})


def _keep_unsent(node: MaintenanceNode, results: list[dict[str, Any]]) -> None:
    path = node.state_dir / "unsent.json"
    node._write(path, node._read(path, []) + list(results))


def poll_once(node: MaintenanceNode, client: EdgeMailboxClient) -> dict[str, Any]:
    """One pass: sweep expiries, fetch new requests, act, post what changed.

    Every outcome is written to ``unsent.json`` the moment it exists, before
    any network call that could fail, and leaves it only once the relay has
    accepted it. A request that ran is already marked seen, so a result lost
    to a failed post could never be produced again.
    """
    cursor_path = node.state_dir / "cursor.json"
    after = int(node._read(cursor_path, {}).get("after", 0))
    _keep_unsent(node, node.sweep())
    page = client.fetch(after)
    for item in page.get("requests", []):
        if "approval" in item:
            _keep_unsent(node, [node.redeem(item.get("approval") or {})])
        else:
            _keep_unsent(node, [node.receive(item.get("envelope") or {})])
        after = max(after, int(item.get("seq", after)))
        node._write(cursor_path, {"after": after})
    unsent = node._read(node.state_dir / "unsent.json", [])
    if unsent:
        client.post_results(unsent)
        # Only what was sent leaves: an approval at the command line may have
        # added one while the post was in flight.
        remaining = node._read(node.state_dir / "unsent.json", [])[len(unsent):]
        node._write(node.state_dir / "unsent.json", remaining)
    return {"after": after, "processed": len(page.get("requests", [])), "reported": len(unsent)}


def report(node: MaintenanceNode, client: EdgeMailboxClient, result: dict[str, Any]) -> None:
    """Post one result now (after a local approve or deny), or keep it for the next pass."""
    try:
        client.post_results([result])
    except OSError:
        _keep_unsent(node, [result])


__all__ = [
    "ACTIONS",
    "APPROVAL_RULES",
    "LEVELS",
    "ActionSpec",
    "DECISIONS",
    "approval_link",
    "link_token",
    "read_link_token",
    "request_digest",
    "sign_approval",
    "EdgeMailboxClient",
    "MaintenanceNode",
    "Refused",
    "b64",
    "unb64",
    "canonical",
    "heartbeat_section",
    "load_wiring",
    "node_from_config",
    "poll_once",
    "report",
    "sign_request",
]
