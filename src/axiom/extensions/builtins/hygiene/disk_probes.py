# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Read a real node for the disk-pressure ladder: filesystems, the kubelet, images.

Every probe is read-only and bounded (a few seconds each), and every one
degrades to "could not read" rather than raising: the ladder must still report
the volumes it can see when the cluster API is down, which is exactly when a
full disk has taken the database with it.

The kubelet's thresholds come from its own ``configz`` endpoint, reached
through the API server (``kubectl get --raw /api/v1/nodes/<node>/proxy/configz``),
because the configuration file on disk is not what is running until the
kubelet restarts. The images a node holds come from the node's status
(``status.images``) and, when that list is truncated, from the runtime itself.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import disk_ladder as dl

#: Seconds any one probe may take.
PROBE_TIMEOUT_S = 10

#: Where a site's ladder configuration lives when no path is given.
CONFIG_ENV = "AXIOM_DISK_LADDER_CONFIG"
#: Who is told, when the configuration does not say (a principal or a vault reference).
ALERT_TO_ENV = "AXIOM_DISK_LADDER_ALERT_TO"

#: The kubelet lists at most this many images in node status by default.
_NODE_STATUS_MAX_IMAGES = 50


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S, check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return proc.returncode, proc.stdout


def _json(argv: list[str]) -> Any | None:
    rc, out = _run(argv)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def docker_root() -> str:
    """The container runtime's data root, or "" when docker is not here."""
    if not shutil.which("docker"):
        return ""
    rc, out = _run(["docker", "info", "--format", "{{.DockerRootDir}}"])
    return out.strip() if rc == 0 else ""


#: The docker daemon's configuration, readable without the socket.
DAEMON_JSON = Path("/etc/docker/daemon.json")
#: The mount table, readable by anyone on Linux.
PROC_MOUNTS = Path("/proc/mounts")
#: Where dockerd keeps its data when ``daemon.json`` does not say.
DOCKER_DEFAULT_ROOT = "/var/lib/docker"


@dataclass
class DataRoot:
    """The runtime's data root, how it was found, and what was tried."""

    root: str = ""
    via: str = ""
    tried: list[str] = field(default_factory=list)


def _daemon_json_root() -> str | None:
    """``data-root`` from ``/etc/docker/daemon.json``; the default root when the file
    exists without one; None when it cannot be read."""
    try:
        data = json.loads(DAEMON_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return str(data.get("data-root") or data.get("graph") or DOCKER_DEFAULT_ROOT).rstrip("/")


def _mount_table() -> list[tuple[str, str, str]]:
    """(device, mountpoint, options) per line of ``/proc/mounts``; [] when unreadable."""
    try:
        text = PROC_MOUNTS.read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            point = parts[1].replace("\\040", " ")
            rows.append((parts[0], point, parts[3]))
    return rows


def _root_before(path: str, marker: str) -> str:
    i = path.find(marker)
    return path[:i] if i > 0 else ""


def _mounts_root() -> str:
    """The data root under a running container's overlay mount (``<root>/overlay2/…``)."""
    for _dev, point, _opts in _mount_table():
        root = _root_before(point, "/overlay2/")
        if root:
            return root
    return ""


def _k3d_mount_sources() -> list[str]:
    """Host sources of the k3d node containers' volumes, when docker answers."""
    if not shutil.which("docker"):
        return []
    rc, out = _run(["docker", "ps", "-q", "--filter", "label=app=k3d"])
    ids = out.split() if rc == 0 else []
    if not ids:
        return []
    data = _json(["docker", "inspect", *ids]) or []
    return [str(m.get("Source", "")) for c in data for m in (c.get("Mounts") or [])
            if m.get("Type") == "volume" and m.get("Source")]


def resolve_docker_data_root() -> DataRoot:
    """The container runtime's data root, by whatever on this node can say.

    ``docker info`` needs the socket, which a node's own user often cannot open.
    Then the daemon's configuration file, then the overlay mounts of running
    containers in the mount table, then the k3d node containers' volume
    sources. Nothing answering is "unresolved", never a guess at another path.
    """
    found = DataRoot()
    found.tried.append("docker info")
    root = docker_root()
    if root:
        found.root, found.via = root, "docker info"
        return found
    found.tried.append(str(DAEMON_JSON.name) + f" ({DAEMON_JSON})")
    root = _daemon_json_root()
    if root:
        found.root, found.via = root, str(DAEMON_JSON)
        return found
    found.tried.append(f"container overlay mounts ({PROC_MOUNTS})")
    root = _mounts_root()
    if root:
        found.root, found.via = root, f"container overlay mounts ({PROC_MOUNTS})"
        return found
    found.tried.append("k3d node volume sources (docker inspect)")
    for src in _k3d_mount_sources():
        root = _root_before(src, "/volumes/")
        if root:
            found.root, found.via = root, "k3d node volume sources"
            return found
    return found


def _unresolved_reason(found: DataRoot) -> str:
    return ("the container runtime's data root: tried " + ", ".join(found.tried)
            + "; none answered")


def default_config() -> dl.LadderConfig:
    """What to watch when the site says nothing: the runtime's data root, or ``/``.

    The data root carries both kubelet disk signals because a k3d node's
    ``nodefs`` and ``imagefs`` live inside a container on it. A node with a
    container runtime whose data root cannot be found is reported unresolved:
    measuring ``/var/lib/rancher/k3s`` or ``/`` instead is how a node with 84 GB
    free on its data root was once called critical.
    """
    signals = ("nodefs", "imagefs") if shutil.which("kubectl") else ()
    found = resolve_docker_data_root()
    has_runtime = bool(shutil.which("docker")) or DAEMON_JSON.exists()
    if found.root:
        vols = [dl.VolumeSpec(name="container-data-root", path=found.root, signals=signals)]
    elif has_runtime:
        vols = [dl.VolumeSpec(name="container-data-root", path=AUTO_DOCKER_ROOT,
                              signals=signals, unresolved=_unresolved_reason(found))]
    elif Path("/var/lib/rancher/k3s").exists():
        vols = [dl.VolumeSpec(name="k3s-data", path="/var/lib/rancher/k3s", signals=signals)]
    else:
        vols = [dl.VolumeSpec(name="root", path="/", signals=())]
    return dl.LadderConfig(volumes=vols, source="default")


def _config_candidates(path: str | None) -> list[str]:
    """``path``, the environment, then ``config/disk-ladder.toml`` (where a site's
    deploy installs it) and ``disk-ladder.toml`` under the platform's state
    directory and the brand's. The timer names the file in its environment; a
    person typing the verb, under either brand, finds the same one."""
    from axiom.infra.paths import get_platform_state_dir, get_user_state_dir

    out: list[str] = []
    for c in (path, os.environ.get(CONFIG_ENV, "").strip() or None):
        if c:
            out.append(c)
    for base in (Path(get_platform_state_dir()), Path(get_user_state_dir())):
        for rel in ("config/disk-ladder.toml", "disk-ladder.toml"):
            c = str(base / rel)
            if c not in out:
                out.append(c)
    return out


def resolve_config(path: str | None = None) -> dl.LadderConfig:
    """The site's configuration: ``path``, else ``$AXIOM_DISK_LADDER_CONFIG``, else the
    installed site config (:func:`_config_candidates`), else :func:`default_config`."""
    candidates = _config_candidates(path)
    for c in candidates:
        if c and Path(c).is_file():
            cfg = dl.load_config(c)
            if not cfg.volumes:
                cfg.volumes = default_config().volumes
            cfg.volumes = [_resolve_auto(v) for v in cfg.volumes]
            return cfg
        if c and c == path:
            raise FileNotFoundError(f"no disk-ladder configuration at {c}")
    return default_config()


#: A volume path the probes resolve on the node: the container runtime's data root.
AUTO_DOCKER_ROOT = "auto:docker-data-root"


def _resolve_auto(spec: dl.VolumeSpec) -> dl.VolumeSpec:
    """``path = "auto:docker-data-root"`` becomes the runtime's real data root, so a
    site's config need not hardcode where the runtime keeps it (or follow a move)."""
    if spec.path == AUTO_DOCKER_ROOT:
        found = resolve_docker_data_root()
        if found.root:
            return dl.VolumeSpec(name=spec.name, path=found.root, signals=spec.signals)
        return dl.VolumeSpec(name=spec.name, path=spec.path, signals=spec.signals,
                             unresolved=_unresolved_reason(found))
    return spec


def default_recipient() -> str:
    """``$AXIOM_DISK_LADDER_ALERT_TO`` (a principal or a vault reference), else the
    owner in this node's identity, else the principal running this node."""
    raw = os.environ.get(ALERT_TO_ENV, "").strip()
    if raw and "://" in raw:
        from axiom.extensions.builtins.secrets import SecretRef, resolve

        with resolve(SecretRef.parse(raw)) as secret:
            raw = secret.as_str().strip()
    if raw:
        return raw
    from axiom.infra.paths import get_user_state_dir

    try:
        ident = json.loads(
            (Path(get_user_state_dir()) / "identity" / "identity.json").read_text("utf-8")
        )
        owner = str(ident.get("owner") or "").strip()
    except (OSError, ValueError):
        owner = ""
    if owner:
        return owner if owner.startswith("@") else f"@{owner.split('@', 1)[0]}"
    from axiom.infra.principal import canonical_principal, local_handle

    return canonical_principal(local_handle())


def state_path() -> Path:
    """The saved reading. Machine state, so the platform's directory under every
    brand: the timer runs ``axi`` and a person types ``neut status``, and both
    must see one reading (``~/.neut`` and ``~/.axi`` once disagreed)."""
    from axiom.infra.paths import get_platform_state_dir

    return Path(get_platform_state_dir()) / "hygiene" / "disk-ladder.json"


def _mountpoint_of(path: str) -> str:
    """The longest mountpoint in the mount table that contains ``path``; "" if none."""
    best = ""
    p = path.rstrip("/") or "/"
    for _dev, point, _opts in _mount_table():
        inside = point == "/" or p == point or p.startswith(point.rstrip("/") + "/")
        if inside and len(point) > len(best):
            best = point
    return best

class NodeProbes:
    """:class:`disk_ladder.Probes` against this machine. Read-only."""

    def usage(self, path: str) -> tuple[int, int]:
        """(total, free) of the filesystem holding ``path``.

        A path behind a directory this user may not search (a docker volume
        under a root-only data root) is measured at its mountpoint from the
        mount table: the same filesystem, so the same numbers.
        """
        try:
            u = shutil.disk_usage(path)
        except PermissionError:
            point = _mountpoint_of(path)
            if not point:
                raise
            u = shutil.disk_usage(point)
        return u.total, u.free


    def _kubectl(self, cfg: dl.LadderConfig, *args: str) -> list[str]:
        argv = ["kubectl"]
        if cfg.kubectl_context:
            argv += ["--context", cfg.kubectl_context]
        argv += ["--request-timeout", f"{PROBE_TIMEOUT_S}s", *args]
        return argv

    def kubelet(self, cfg: dl.LadderConfig) -> dl.KubeletView:
        if not shutil.which("kubectl"):
            has_cluster = bool(shutil.which("k3d") or shutil.which("k3s"))
            return dl.KubeletView(
                policy=dl.EvictionPolicy.unread() if has_cluster else dl.EvictionPolicy.none(),
                findings=["kubectl not found; the kubelet's thresholds were not read"]
                if has_cluster else [],
            )
        nodes = _json(self._kubectl(cfg, "get", "nodes", "-o", "json"))
        items = (nodes or {}).get("items") or []
        if not items:
            return dl.KubeletView(
                policy=dl.EvictionPolicy.unread(),
                findings=["the cluster API did not answer; kubelet thresholds and images unread"],
            )
        node = _pick_node(items)
        name = node.get("metadata", {}).get("name", "")
        configz = _json(self._kubectl(cfg, "get", "--raw",
                                      f"/api/v1/nodes/{name}/proxy/configz"))
        policy = (dl.EvictionPolicy.from_configz(configz) if isinstance(configz, dict)
                  else dl.EvictionPolicy.unread())
        pressure = _disk_pressure(node)
        images = self._never_pull_images(cfg, node)
        return dl.KubeletView(policy=policy, pressure_on=pressure, images=images, node=name)

    def _never_pull_images(self, cfg: dl.LadderConfig, node: dict) -> list[dl.ImageCheck]:
        pods = _json(self._kubectl(cfg, "get", "pods", "-A", "-o", "json")) or {}
        wanted = set(cfg.images)
        for pod in pods.get("items") or []:
            spec = pod.get("spec") or {}
            for c in (spec.get("containers") or []) + (spec.get("initContainers") or []):
                if c.get("imagePullPolicy") == "Never" and c.get("image"):
                    wanted.add(c["image"])
        if not wanted:
            return []
        listed = _node_status_images(node)
        truncated = len(node.get("status", {}).get("images") or []) >= _NODE_STATUS_MAX_IMAGES
        runtime: set[str] | None = None
        out = []
        for image in sorted(wanted):
            on_node: bool | None = _matches(image, listed)
            if not on_node and truncated:
                if runtime is None:
                    runtime = _runtime_images(node.get("metadata", {}).get("name", ""))
                on_node = _matches(image, runtime) if runtime is not None else None
            out.append(dl.ImageCheck(image=image, on_node=on_node, on_host=_host_has(image)))
        return out


def _pick_node(items: list[dict]) -> dict:
    """This host's node when it can be told apart, else the first (a one-node cluster)."""
    host = socket.gethostname().split(".", 1)[0]
    for item in items:
        name = item.get("metadata", {}).get("name", "")
        if name == host or name.startswith(f"{host}."):
            return item
    return items[0]


def _disk_pressure(node: dict) -> bool | None:
    for cond in node.get("status", {}).get("conditions") or []:
        if cond.get("type") == "DiskPressure":
            return str(cond.get("status")) == "True"
    return None


def _node_status_images(node: dict) -> set[str]:
    names: set[str] = set()
    for entry in node.get("status", {}).get("images") or []:
        names.update(entry.get("names") or [])
    return names


def _normal(ref: str) -> set[str]:
    """The spellings one image reference goes by (``docker.io/library/x:t`` is ``x:t``)."""
    forms = {ref}
    if ":" not in ref.rsplit("/", 1)[-1] and "@" not in ref:
        forms.add(f"{ref}:latest")
    for prefix in ("docker.io/library/", "docker.io/"):
        if ref.startswith(prefix):
            forms.add(ref[len(prefix):])
    if "/" not in ref:
        forms.add(f"docker.io/library/{ref}")
    return forms


def _matches(image: str, present: set[str]) -> bool:
    want = _normal(image)
    return any(_normal(p) & want for p in present)


def _runtime_images(node_name: str) -> set[str] | None:
    """Images in the node's runtime: inside a k3d node container, else ``crictl``."""
    if node_name and shutil.which("docker"):
        data = _json(["docker", "exec", node_name, "crictl", "images", "-o", "json"])
        if data is not None:
            return {t for img in data.get("images") or [] for t in img.get("repoTags") or []}
    if shutil.which("crictl"):
        data = _json(["crictl", "images", "-o", "json"])
        if data is not None:
            return {t for img in data.get("images") or [] for t in img.get("repoTags") or []}
    return None


def _host_has(image: str) -> bool | None:
    """Whether the host's docker still holds ``image`` (the source for a re-import)."""
    if not shutil.which("docker"):
        return None
    rc, _ = _run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    return rc == 0


def inbox_notifier(recipient: str, *, actor: str = "@disk-ladder:platform"):
    """A ``notify`` that writes the durable inbox row the notifications gateway delivers."""

    def _notify(alert: dict[str, Any]) -> None:
        from axiom.extensions.builtins.notifications.inbox_db import DatabaseInboxStore

        summary = alert["summary"]
        if alert.get("body"):
            summary = f"{summary}\n\n{alert['body']}"
        DatabaseInboxStore().write_alert(
            recipient=alert.get("recipient") or recipient,
            summary=summary,
            priority=alert["priority"],
            classification="internal",
            actor=actor,
            link="",
            dedup_key=alert["dedup_key"],
        )

    return _notify


__all__ = [
    "ALERT_TO_ENV",
    "AUTO_DOCKER_ROOT",
    "CONFIG_ENV",
    "DataRoot",
    "NodeProbes",
    "default_config",
    "default_recipient",
    "inbox_notifier",
    "resolve_config",
    "resolve_docker_data_root",
    "state_path",
]
