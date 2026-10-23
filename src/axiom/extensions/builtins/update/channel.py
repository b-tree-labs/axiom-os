# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A release channel: which versions a node may take, signed by the platform operator (ADR-179 §3).

A version being on a package index does not make it a version partners take.
The operator publishes a small manifest per channel, naming the versions a
node class may install, each with its release notes and the sha256 of its
wheel, and signs it. It is served from the public intake host, which every
node already reaches, so a node polls it over an outbound connection.

A node accepts a manifest only if:

- it is signed by a release key the node trusts;
- it names this channel;
- it has not expired (a manifest says how long it may be relied on);
- it is not older than the newest manifest this node has accepted, so a
  replayed old manifest cannot hold a node on an old version or walk it back.

Then the node takes only a version the manifest lists, newer than the one it
runs, as its update policy allows (:func:`swap.decide`), and installs only a
wheel whose sha256 matches the manifest. The index is where bytes come from;
the manifest is what says which bytes are acceptable.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from axiom.infra.maintenance import b64, canonical, unb64
from axiom.vega.identity.keypair import Keypair, verify

from . import swap

#: Where the edge serves channels from; one ``<channel>.json`` per channel.
DIR_ENV = "AXIOM_RELEASE_CHANNEL_DIR"
#: The longest a manifest may be relied on after it is issued.
MAX_VALIDITY = timedelta(days=30)
SKEW = timedelta(minutes=5)


class ChannelRefused(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def sign_manifest(
    keypair: Keypair,
    *,
    key_id: str,
    channel: str,
    package: str,
    releases: list[dict[str, Any]],
    issued_at: datetime | None = None,
    valid_for: timedelta = timedelta(days=14),
) -> dict[str, Any]:
    """A signed channel manifest. Each release: ``version``, ``sha256`` (of the wheel), ``notes``."""
    issued_at = issued_at or datetime.now(UTC)
    for r in releases:
        if not {"version", "sha256"} <= set(r):
            raise ValueError("every release needs a version and the sha256 of its wheel")
    manifest = {
        "kind": "release-channel", "channel": channel, "package": package, "key_id": key_id,
        "issued_at": _iso(issued_at), "expires_at": _iso(issued_at + valid_for),
        "releases": [dict(r) for r in releases],
    }
    return {"manifest": manifest, "signature": b64(keypair.sign(canonical(manifest)))}


def publish(envelope: dict[str, Any], directory: Path) -> Path:
    """Write a signed manifest where the edge serves it."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{envelope['manifest']['channel']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


@dataclass
class ChannelNode:
    """The node side: verify a manifest, remember the newest, pick what to install."""

    channel: str
    package: str
    trusted_keys: dict[str, str]  # key_id -> base64 public key
    state_dir: Path

    @property
    def _state(self) -> Path:
        return Path(self.state_dir) / f"channel-{self.channel}.json"

    def _last_issued(self) -> datetime | None:
        try:
            return _parse(json.loads(self._state.read_text(encoding="utf-8"))["issued_at"])
        except (OSError, ValueError, KeyError):
            return None

    def verify(self, envelope: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        manifest = envelope.get("manifest")
        if not isinstance(manifest, dict) or manifest.get("kind") != "release-channel":
            raise ChannelRefused("malformed", "not a release-channel manifest")
        key = self.trusted_keys.get(str(manifest.get("key_id") or ""))
        if not key:
            raise ChannelRefused("unknown_key", "signed with a key this node does not trust")
        try:
            ok = verify(unb64(key), canonical(manifest), unb64(str(envelope.get("signature") or "")))
        except (ValueError, KeyError):
            ok = False
        if not ok:
            raise ChannelRefused("bad_signature", "the signature does not match the manifest")
        if manifest.get("channel") != self.channel or manifest.get("package") != self.package:
            raise ChannelRefused("wrong_channel", f"for {manifest.get('channel')}/{manifest.get('package')}")
        try:
            issued, expires = _parse(manifest["issued_at"]), _parse(manifest["expires_at"])
        except (KeyError, ValueError) as exc:
            raise ChannelRefused("malformed", f"bad validity window: {exc}") from exc
        if expires - issued > MAX_VALIDITY:
            raise ChannelRefused("validity_too_long", f"a manifest may be valid for at most {MAX_VALIDITY}")
        if issued - SKEW > now:
            raise ChannelRefused("not_yet_valid", "issued in the future (check the clocks)")
        if now > expires + SKEW:
            raise ChannelRefused("expired", "this manifest has expired; the operator publishes a fresh one")
        last = self._last_issued()
        if last is not None and issued < last:
            raise ChannelRefused("rolled_back", "older than a manifest this node already accepted")
        Path(self.state_dir).mkdir(parents=True, exist_ok=True)
        self._state.write_text(json.dumps({"issued_at": manifest["issued_at"]}), encoding="utf-8")
        return manifest

    def fetch(self, base_url: str, *, timeout_s: float = 30.0) -> dict[str, Any]:
        url = f"{base_url.rstrip('/')}/releases/{self.channel}.json"
        with urllib.request.urlopen(url, timeout=timeout_s) as r:  # noqa: S310 - configured intake host
            return self.verify(json.loads(r.read()))

    def choose(self, manifest: dict[str, Any], current: str, *, policy: str, approved: set[str] = frozenset(),
               now: datetime | None = None, window=None) -> tuple[str, dict[str, Any] | None]:
        """``(decision, release)``: the newest listed release the policy would take, and what to do with it."""
        listed = sorted(manifest["releases"], key=lambda r: swap._parse(r["version"]), reverse=True)
        for rel in listed:
            d = swap.decide(policy, current, rel["version"], approved=rel["version"] in approved, now=now, window=window)
            if d in ("apply", "wait"):
                return d, rel
        newest = listed[0] if listed else None
        if newest and swap._parse(newest["version"]) > swap._parse(current):
            return ("hold" if policy == "hold" else "ask"), newest
        return "current", None

    def notes_between(self, manifest: dict[str, Any], current: str, target: str) -> str:
        """The release notes of every listed version after ``current`` up to ``target``."""
        lo, hi = swap._parse(current), swap._parse(target)
        rels = sorted((r for r in manifest["releases"] if lo < swap._parse(r["version"]) <= hi),
                      key=lambda r: swap._parse(r["version"]))
        return "\n\n".join(f"{r['version']}\n{r.get('notes', '').strip()}" for r in rels)


def download_verified(package: str, release: dict[str, Any], dest: Path, *, index_url: str | None = None,
                      find_links: Path | None = None, python: str | None = None, timeout_s: float = 300.0) -> Path:
    """Fetch the release's wheel (no dependencies) and check its sha256 against the manifest."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [python or sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary", ":all:",
           "--disable-pip-version-check", "-q", "-d", str(dest), f"{package}=={release['version']}"]
    if find_links is not None:
        cmd[4:4] = ["--no-index", "--find-links", str(find_links)]
    elif index_url:
        cmd[4:4] = ["--index-url", index_url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    if r.returncode != 0:
        lines = (r.stderr or r.stdout or "").strip().splitlines()
        raise ChannelRefused("download_failed", lines[-1] if lines else "pip download failed")
    norm = package.replace("-", "_").lower()
    wheels = [p for p in dest.glob("*.whl") if p.name.lower().startswith(f"{norm}-{release['version']}-")]
    if len(wheels) != 1:
        raise ChannelRefused("download_failed", f"expected one wheel for {package} {release['version']}")
    digest = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
    if digest != str(release["sha256"]).lower():
        wheels[0].unlink(missing_ok=True)
        raise ChannelRefused("hash_mismatch", f"{wheels[0].name} is {digest[:12]}…, the manifest says "
                                              f"{str(release['sha256'])[:12]}…")
    return wheels[0]


def update_from_channel(
    node: ChannelNode,
    base_url: str,
    root: Path,
    *,
    policy: str = "auto-patch",
    approved: set[str] = frozenset(),
    checks: list[list[str]],
    index_url: str | None = None,
    find_links: Path | None = None,
    window=None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One pass: fetch and verify the manifest, choose, download and verify the wheel, swap.

    Never raises for a refusal: the result says what happened, for the heartbeat.
    """
    current = swap.current_version(root) or "0"
    try:
        manifest = node.fetch(base_url)
    except ChannelRefused as exc:
        return {"status": "channel_refused", "reason": exc.reason, "detail": exc.detail}
    except OSError as exc:
        return {"status": "channel_unreachable", "detail": str(exc)}
    decision, release = node.choose(manifest, current, policy=policy, approved=approved, now=now, window=window)
    if decision != "apply" or release is None:
        out: dict[str, Any] = {"status": decision, "current": current}
        if release is not None:
            out.update(version=release["version"], notes=node.notes_between(manifest, current, release["version"]))
        return out
    with tempfile.TemporaryDirectory() as tmp:
        try:
            wheel = download_verified(node.package, release, Path(tmp), index_url=index_url, find_links=find_links)
        except ChannelRefused as exc:
            return {"status": "refused", "reason": exc.reason, "detail": exc.detail, "version": release["version"]}
        outcome = swap.apply_update(root, str(wheel), release["version"], checks=checks,
                                    index_url=index_url, find_links=find_links)
    return {"status": outcome.status, "from": outcome.from_version, "to": outcome.to_version, "detail": outcome.detail}


def build_channel_router(directory: Path):
    """``/releases/<channel>.json`` — the signed manifests, public: the signature is what a node trusts."""
    import re

    from fastapi import APIRouter, HTTPException
    from fastapi.responses import Response

    router = APIRouter()

    @router.get("/releases/{name}")
    def manifest(name: str) -> Response:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}\.json", name):
            raise HTTPException(status_code=404, detail="no such channel")
        path = Path(directory) / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="no such channel")
        return Response(content=path.read_bytes(), media_type="application/json",
                        headers={"Cache-Control": "no-cache"})

    return router


# -- the node's scheduled check ---------------------------------------------------
#
# A node checks its channel from the service that already runs on a schedule
# (the maintenance poll), rather than from a second daemon. What it found is
# kept for the heartbeat, so a release waiting for approval is visible to the
# platform operator, who starts the approval notice (see ``update.notice``).

#: The deployment's channel settings live under ``[update]`` in the maintenance wiring.
WIRING_TABLE = "update"
LAST_FILE = "update-channel.json"
#: How often a node checks its channel, by default.
DEFAULT_CHECK_EVERY_S = 3600.0


def _window(text: str | None):
    if not text:
        return None
    from datetime import time as _time

    start, _, end = str(text).partition("-")
    h1, m1 = (int(x) for x in start.strip().split(":"))
    h2, m2 = (int(x) for x in end.strip().split(":"))
    return (_time(h1, m1), _time(h2, m2))


def settings_from_wiring(wiring: dict[str, Any]) -> dict[str, Any] | None:
    """The ``[update]`` table, checked, or ``None`` when this node takes no channel."""
    cfg = wiring.get(WIRING_TABLE)
    if not isinstance(cfg, dict):
        return None
    missing = [k for k in ("channel_url", "channel", "package", "root", "release_keys") if not cfg.get(k)]
    if missing:
        raise ValueError(f"[{WIRING_TABLE}] is missing {', '.join(missing)}")
    return cfg


def _node_for(cfg: dict[str, Any], state_dir: Path) -> ChannelNode:
    return ChannelNode(channel=str(cfg["channel"]), package=str(cfg["package"]),
                       trusted_keys={str(k): str(v) for k, v in dict(cfg["release_keys"]).items()},
                       state_dir=Path(state_dir))


def _checks(cfg: dict[str, Any]) -> list[list[str]]:
    return [[str(t) for t in c] for c in (cfg.get("checks") or [])] or [["python", "-c", "pass"]]


def last_check(state_dir: Path) -> dict[str, Any] | None:
    try:
        return json.loads((Path(state_dir) / LAST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def channel_tick(wiring: dict[str, Any], state_dir: Path, *, force: bool = False,
                 now: datetime | None = None) -> dict[str, Any] | None:
    """One scheduled check, if one is due. ``None`` when not configured or not due.

    Applies what the policy allows without a person (by default, fixes inside
    the window). A release that needs approval is recorded, with the notes of
    every release in between, for the heartbeat. Never raises.
    """
    import time as _time

    try:
        cfg = settings_from_wiring(wiring)
    except ValueError as exc:
        return {"status": "misconfigured", "detail": str(exc)}
    if cfg is None:
        return None
    state_dir = Path(state_dir)
    prev = last_check(state_dir) or {}
    every = float(cfg.get("check_every_s", DEFAULT_CHECK_EVERY_S))
    if not force and _time.time() - float(prev.get("checked_epoch", 0)) < every:
        return None
    try:
        out = update_from_channel(
            _node_for(cfg, state_dir), str(cfg["channel_url"]), Path(cfg["root"]),
            policy=str(cfg.get("policy") or "auto-patch"), checks=_checks(cfg),
            index_url=cfg.get("index_url"), find_links=cfg.get("find_links"),
            window=_window(cfg.get("window")), now=now)
    except Exception as exc:  # noqa: BLE001 - a scheduled check must never take the service down
        out = {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
    out = {**out, "checked_at": _iso(datetime.now(UTC)), "checked_epoch": _time.time(),
           "current": swap.current_version(Path(cfg["root"]))}
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / (LAST_FILE + ".tmp")
    tmp.write_text(json.dumps(out), encoding="utf-8")
    tmp.replace(state_dir / LAST_FILE)
    return out


def apply_approved(wiring: dict[str, Any], state_dir: Path, version: str) -> dict[str, Any]:
    """Install ``version`` from the channel because a person approved it.

    Still only a version the signed channel lists, with the wheel it names, and
    with the same checks and rollback. Not held to the window: the approval was
    for now, and the request it came with expires.
    """
    cfg = settings_from_wiring(wiring)
    if cfg is None:
        return {"status": "misconfigured", "detail": f"no [{WIRING_TABLE}] in the wiring"}
    node = _node_for(cfg, Path(state_dir))
    root = Path(cfg["root"])
    try:
        manifest = node.fetch(str(cfg["channel_url"]))
    except ChannelRefused as exc:
        return {"status": "channel_refused", "reason": exc.reason, "detail": exc.detail}
    release = next((r for r in manifest["releases"] if r["version"] == version), None)
    if release is None:
        return {"status": "not_on_channel", "detail": f"{version} is not on channel {node.channel}"}
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        try:
            wheel = download_verified(node.package, release, Path(tmp), index_url=cfg.get("index_url"),
                                      find_links=cfg.get("find_links"))
        except ChannelRefused as exc:
            return {"status": "refused", "reason": exc.reason, "detail": exc.detail}
        outcome = swap.apply_update(root, str(wheel), version, checks=_checks(cfg), index_url=cfg.get("index_url"),
                                    find_links=cfg.get("find_links"))
    return {"status": outcome.status, "from": outcome.from_version, "to": outcome.to_version,
            "detail": outcome.detail}


__all__ = ["DIR_ENV", "apply_approved", "build_channel_router", "channel_tick", "last_check",
           "settings_from_wiring", "ChannelNode", "ChannelRefused", "download_verified", "publish", "sign_manifest",
           "update_from_channel"]
