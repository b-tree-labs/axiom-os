# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Node discovery — find and track federation peers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

try:
    import yaml  # type: ignore[import-untyped]

    _HAS_YAML = True
except ImportError:  # pragma: no cover
    _HAS_YAML = False

_DEFAULT_REGISTRY_PATH = Path.home() / ".axi" / "nodes.yaml"

# Minimum axi version on the peer for identity binding. `axi federation
# status --json` landed in 0.10.4; older peers emit "unknown subcommand"
# which is a terrible first-run experience. Preflight-check and guide
# the operator to run `axi update` on the peer.
MIN_PEER_VERSION_FOR_IDENTITY_BINDING = "0.10.4"


def _parse_version(raw: str) -> tuple[int, ...] | None:
    """Parse ``axi X.Y.Z`` or ``X.Y.Z`` into a comparable tuple.

    Returns None if no dotted numeric version is found.
    """
    import re

    m = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


class NodeState(Enum):
    """Lifecycle states for a known peer node."""

    UNKNOWN = "unknown"
    DISCOVERED = "discovered"
    VERIFIED = "verified"
    TRUSTED = "trusted"
    FEDERATED = "federated"
    UNREACHABLE = "unreachable"
    LEAVING = "leaving"
    EVICTED = "evicted"


@dataclass
class KnownNode:
    """A remote node we know about.

    Identity fields (public_key, owner, fingerprint, identity_verified_at)
    are populated once we've performed an identity-fetch from the peer.
    Until then, node_id is a transport-derived placeholder and state is
    DISCOVERED, not VERIFIED. This keeps the transport contact (you can
    reach them) separate from the identity binding (you know who they are
    cryptographically) — critical for scaling to hierarchical federations
    where the same identity may be reachable via many transports.
    """

    node_id: str
    display_name: str
    url: str  # A2A endpoint or SSH host
    transport: str = "ssh"  # "ssh", "a2a", "mdns"
    state: NodeState = NodeState.UNKNOWN
    profile: str = "standard"
    capabilities: list[str] = field(default_factory=list)
    last_seen: str = ""  # ISO 8601
    trust_level: str = "untrusted"
    ssh_user: str = ""
    ssh_host: str = ""

    # Identity binding — populated by fetch_identity(). Empty until then.
    public_key: str = ""  # base64(raw Ed25519 pubkey)
    owner: str = ""  # e.g. "user@example.org"
    fingerprint: str = ""  # SHA-256 of pubkey, grouped for human comparison
    identity_verified_at: str = ""  # ISO 8601 of the last successful fetch

    def to_dict(self) -> dict:
        """Serialise to a plain ``dict`` (YAML-friendly)."""
        return {
            "node_id": self.node_id,
            "display_name": self.display_name,
            "url": self.url,
            "transport": self.transport,
            "state": self.state.value,
            "profile": self.profile,
            "capabilities": self.capabilities,
            "last_seen": self.last_seen,
            "trust_level": self.trust_level,
            "ssh_user": self.ssh_user,
            "ssh_host": self.ssh_host,
            "public_key": self.public_key,
            "owner": self.owner,
            "fingerprint": self.fingerprint,
            "identity_verified_at": self.identity_verified_at,
        }

    @property
    def has_verified_identity(self) -> bool:
        return bool(self.public_key and self.fingerprint)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class NodeRegistry:
    """Local registry of known nodes — persisted to ``~/.axi/nodes.yaml``."""

    def __init__(self, registry_path: Path | None = None) -> None:
        self._path = registry_path or _DEFAULT_REGISTRY_PATH
        self._nodes: dict[str, KnownNode] = {}
        if self._path.exists():
            self.load()

    # -- CRUD ---------------------------------------------------------------

    def add(self, node: KnownNode) -> None:
        """Add or overwrite a node entry."""
        self._nodes[node.node_id] = node

    def remove(self, node_id: str) -> bool:
        """Remove a node. Returns ``True`` if it existed."""
        return self._nodes.pop(node_id, None) is not None

    def get(self, node_id: str) -> KnownNode | None:
        """Lookup by *node_id*."""
        return self._nodes.get(node_id)

    def list_all(self) -> list[KnownNode]:
        """Return all known nodes."""
        return list(self._nodes.values())

    def update_state(self, node_id: str, state: NodeState) -> None:
        """Transition a node to a new state."""
        node = self._nodes.get(node_id)
        if node is None:
            msg = f"Unknown node: {node_id}"
            raise KeyError(msg)
        node.state = state
        node.last_seen = _now_iso()

    # -- Persistence --------------------------------------------------------

    def save(self) -> None:
        """Write registry to YAML."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = [n.to_dict() for n in self._nodes.values()]
        if _HAS_YAML:
            self._path.write_text(yaml.dump(data, default_flow_style=False))
        else:  # pragma: no cover
            import json

            self._path.write_text(json.dumps(data, indent=2))

    def load(self) -> None:
        """Read registry from YAML (or JSON fallback)."""
        if not self._path.exists():
            return
        raw = self._path.read_text()
        if _HAS_YAML:
            entries = yaml.safe_load(raw) or []
        else:  # pragma: no cover
            import json

            entries = json.loads(raw)
        self._nodes.clear()
        for entry in entries:
            entry["state"] = NodeState(entry.get("state", "unknown"))
            self._nodes[entry["node_id"]] = KnownNode(**entry)

    # -- Discovery helpers --------------------------------------------------

    def discover_ssh(self, name: str, user: str, host: str) -> KnownNode:
        """Register a node reachable via SSH — transport-only, no identity yet.

        The returned node has ``state=DISCOVERED`` and an empty public_key.
        Call :meth:`fetch_identity_ssh` next to promote to VERIFIED.
        """
        import hashlib

        # Transport-derived placeholder node_id. When fetch_identity succeeds,
        # this gets replaced by the peer's real node_id (sha256 of pubkey).
        node_id = hashlib.sha256(f"ssh:{user}@{host}".encode()).hexdigest()[:16]
        node = KnownNode(
            node_id=node_id,
            display_name=name,
            url=f"ssh://{user}@{host}",
            transport="ssh",
            state=NodeState.DISCOVERED,
            ssh_user=user,
            ssh_host=host,
            last_seen=_now_iso(),
        )
        self.add(node)
        return node

    def check_peer_version(
        self,
        ssh_user: str,
        ssh_host: str,
        ssh_runner=None,
    ) -> tuple[bool, str, str]:
        """SSH to peer, run `axi --version`, compare to the minimum.

        Returns (ok, peer_version_string, message).
          - ok=True: peer is >= MIN_PEER_VERSION_FOR_IDENTITY_BINDING.
          - ok=False: either peer unreachable, version unparseable, or
            too old. The message distinguishes the three so the operator
            knows what to do.
        """
        if ssh_runner is None:
            ssh_runner = _default_ssh_runner

        rc, out, err = ssh_runner(ssh_user, ssh_host, "axi --version")
        if rc != 0:
            detail = err.strip() or out.strip() or f"rc={rc}"
            return (
                False,
                "",
                (f"peer {ssh_user}@{ssh_host} unreachable or axi not installed: {detail}"),
            )

        peer = _parse_version(out)
        if peer is None:
            return (
                False,
                "",
                (f"could not parse peer axi version from output: {out.strip()!r}"),
            )

        peer_str = ".".join(str(p) for p in peer)
        minimum = _parse_version(MIN_PEER_VERSION_FOR_IDENTITY_BINDING) or (0, 0, 0)
        if peer < minimum:
            return (
                False,
                peer_str,
                (
                    f"peer {ssh_host} is running axi {peer_str} — identity "
                    f"binding requires ≥ {MIN_PEER_VERSION_FOR_IDENTITY_BINDING}.\n"
                    f"Run `axi update` on the peer "
                    f"({ssh_user}@{ssh_host}), then retry."
                ),
            )

        return True, peer_str, f"peer axi {peer_str} OK"

    def fetch_identity_ssh(
        self,
        node_id: str,
        *,
        ssh_runner=None,
        on_key_change: str = "refuse",
    ) -> tuple[bool, str]:
        """SSH into a DISCOVERED peer, capture its real identity, bind it.

        Replaces the transport-derived placeholder node_id with the peer's
        actual (pubkey-derived) node_id. Promotes state DISCOVERED → VERIFIED.

        TOFU semantics: on the first successful fetch we bind whatever key
        the peer presents. On later fetches:
          - ``on_key_change="refuse"`` (default): if the pubkey differs
            from the stored one, REJECT the update and return False.
          - ``on_key_change="accept"``: overwrite (caller has
            out-of-band confirmed the change — key rotation, node rebuild).

        Returns (success, message). Pure side-effectful on the registry.

        ssh_runner is injectable for testing — signature (user, host, cmd)
        -> (returncode, stdout, stderr). Defaults to a subprocess runner.
        """
        import json as _json

        from axiom.vega.federation.identity import fingerprint as _fingerprint

        node = self.get(node_id)
        if node is None:
            return False, f"unknown node_id: {node_id}"
        if not node.ssh_user or not node.ssh_host:
            return False, f"node {node_id} has no ssh transport info"

        if ssh_runner is None:
            ssh_runner = _default_ssh_runner

        # Preflight: verify the peer is running a version that actually
        # supports `axi federation status --json`. Old peers emit
        # cryptic "unknown subcommand" errors; we want a guided fix.
        ok_ver, _peer_version, pre_msg = self.check_peer_version(
            node.ssh_user,
            node.ssh_host,
            ssh_runner=ssh_runner,
        )
        if not ok_ver:
            return False, pre_msg

        rc, out, err = ssh_runner(
            node.ssh_user,
            node.ssh_host,
            "axi federation status --json",
        )
        if rc != 0:
            return False, (f"ssh identity fetch failed (rc={rc}): {err.strip() or out.strip()}")

        try:
            status = _json.loads(out)
        except Exception as exc:
            return False, f"peer returned non-JSON: {exc}"

        if not status.get("initialized"):
            return False, (
                "peer has no federation identity yet — run `axi federation init` on the peer first"
            )

        peer_node_id = status.get("node_id", "")
        peer_pubkey = status.get("public_key", "")
        peer_owner = status.get("owner", "")
        peer_display = status.get("display_name", "")
        peer_profile = status.get("profile", "")

        if not peer_pubkey or not peer_node_id:
            return False, "peer status missing node_id or public_key"

        # TOFU check — key on TRANSPORT identity, not node_id.
        #
        # node_id = sha256(pubkey)[:16], so a rotated key always produces a
        # new node_id. Keying the refusal on node_id therefore misses every
        # real rotation (the registry just accrues a parallel entry). The
        # correct invariant: if a peer reachable at (ssh_user, ssh_host)
        # previously presented key_A and now presents key_B, that's a
        # rotation (or MITM) — refuse loudly unless operator confirmed OOB.
        rotation_victim: KnownNode | None = None
        if node.ssh_user and node.ssh_host:
            for existing in self._nodes.values():
                if (
                    existing.ssh_user == node.ssh_user
                    and existing.ssh_host == node.ssh_host
                    and existing.public_key
                    and existing.public_key != peer_pubkey
                    and existing.node_id
                    != node.node_id  # skip the placeholder we're about to replace
                ):
                    rotation_victim = existing
                    break

        if rotation_victim is not None:
            if on_key_change == "refuse":
                fp_old = _fingerprint(rotation_victim.public_key)
                fp_new = _fingerprint(peer_pubkey)
                return False, (
                    f"KEY ROTATION DETECTED on transport {node.ssh_user}@{node.ssh_host} — "
                    "refusing to overwrite identity.\n"
                    f"  stored fingerprint: {fp_old}\n"
                    f"  peer fingerprint:   {fp_new}\n"
                    "Either the peer rotated keys (expected) or this is a "
                    "MITM attempt (not expected).\n"
                    "Re-run with --confirm-key-change after verifying with "
                    "the peer operator out-of-band."
                )
            # on_key_change=="accept" → retire the old binding
            # (caller has confirmed OOB that rotation is legitimate).
            self._nodes.pop(rotation_victim.node_id, None)

        # Bind identity. Remove the placeholder entry (keyed by transport-hash)
        # and re-add under the real node_id.
        self._nodes.pop(node_id, None)

        bound = KnownNode(
            node_id=peer_node_id,
            display_name=peer_display or node.display_name,
            url=node.url,
            transport=node.transport,
            state=NodeState.VERIFIED,
            profile=peer_profile or node.profile,
            capabilities=node.capabilities,
            last_seen=_now_iso(),
            trust_level="tofu",
            ssh_user=node.ssh_user,
            ssh_host=node.ssh_host,
            public_key=peer_pubkey,
            owner=peer_owner,
            fingerprint=_fingerprint(peer_pubkey),
            identity_verified_at=_now_iso(),
        )
        self.add(bound)
        return True, (f"identity bound: node_id={peer_node_id}, fingerprint={bound.fingerprint}")

    def pubkey_for(self, principal: str) -> bytes | None:
        """Return raw Ed25519 pubkey bytes for a principal, if known.

        Accepts either the principal handle (e.g. ``@laptop:abc123``) or
        the bare node_id. Returns None if the principal isn't known or
        has no verified identity yet.
        """
        import base64 as _b64

        # Normalize: strip "@name:" prefix to get the node_id suffix
        key = principal
        if principal.startswith("@") and ":" in principal:
            key = principal.split(":", 1)[1]

        node = self._nodes.get(key)
        if node is None:
            # Try direct lookup by display_name or iterate
            for n in self._nodes.values():
                if n.node_id == principal or n.display_name == principal:
                    node = n
                    break
        if node is None or not node.public_key:
            return None
        try:
            return _b64.b64decode(node.public_key)
        except Exception:
            return None

    def discover_a2a(self, name: str, url: str) -> KnownNode:
        """Register a node reachable via A2A HTTP."""
        import hashlib

        node_id = hashlib.sha256(f"a2a:{url}".encode()).hexdigest()[:16]
        node = KnownNode(
            node_id=node_id,
            display_name=name,
            url=url,
            transport="a2a",
            state=NodeState.DISCOVERED,
            last_seen=_now_iso(),
        )
        self.add(node)
        return node

    def check_health(self, node_id: str) -> dict:
        """Placeholder health check for a single node."""
        node = self.get(node_id)
        if node is None:
            return {"node_id": node_id, "status": "not_found"}
        return {
            "node_id": node_id,
            "display_name": node.display_name,
            "state": node.state.value,
            "last_seen": node.last_seen,
        }

    def check_all(self) -> list[dict]:
        """Health-check summaries for every known node."""
        return [self.check_health(nid) for nid in self._nodes]


def _default_ssh_runner(user: str, host: str, cmd: str) -> tuple[int, str, str]:
    """Run a remote command via ssh. Used by fetch_identity_ssh.

    Peers commonly install `axi` in a project venv that isn't on the PATH
    for non-interactive SSH (direnv-activated, ~/.profile-sourced, etc.).
    We try a sequence of invocations in order of preference:

      1. PATH-resolved `axi` in a login shell (works if peer has
         installed a ~/.local/bin shim — the recommended setup).
      2. `~/.local/bin/axi` (common user-install prefix).
      3. Glob-search under `~/Projects/*/.venv/bin/axi` (dev checkouts).

    The supported way to make candidate (2) always succeed is for peers to
    run `axi install-shim` once (also run automatically at the end of
    `axi install`).  Once that shim is ubiquitous across installs, the
    filesystem-walking fallback (3) becomes vestigial and can be retired.

    Returns (rc, stdout, stderr) from the FIRST success, or the last
    attempt's failure with combined diagnostics.
    """
    import shlex
    import subprocess

    # Build three invocation variants: PATH-based, user-local shim, venv-find.
    cmd_suffix = cmd.removeprefix("axi ") if cmd.startswith("axi ") else cmd
    _find_oneliner = (
        "axi_bin=$(find ~/Projects ~ -maxdepth 6 -type f -name axi "
        '-path "*/.venv/bin/*" 2>/dev/null | head -1); '
        f'[ -x "$axi_bin" ] && exec "$axi_bin" {cmd_suffix}; exit 127'
    )
    candidates = [
        f"bash -l -c {shlex.quote(cmd)}",
        f"bash -l -c {shlex.quote('~/.local/bin/' + cmd)}",
        f"bash -l -c {shlex.quote(_find_oneliner)}",
    ]
    last_rc, last_out, last_err = 127, "", ""
    for wrapped in candidates:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", f"{user}@{host}", wrapped],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return result.returncode, result.stdout, result.stderr
        last_rc, last_out, last_err = result.returncode, result.stdout, result.stderr
    return last_rc, last_out, last_err
