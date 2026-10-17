# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""mDNS federation discovery — automatic LAN/VPN peer detection.

Advertises this node as an Axiom service on the local network using
mDNS (Bonjour/Avahi). Discovers other Axiom nodes automatically.

Service type: _axiom._tcp.local.
TXT records: node_id, profile, version, federation

Usage::

    svc = MDNSService(node_id="ax-7f3a", port=8766)
    svc.start()       # Advertise + listen
    peers = svc.get_discovered_peers()
    svc.stop()
"""

from __future__ import annotations

import logging
import socket

log = logging.getLogger(__name__)

SERVICE_TYPE = "_axiom._tcp.local."
_ANNOUNCE_INTERVAL = 30  # seconds


class AxiomServiceListener:
    """Listens for Axiom nodes on the network via mDNS."""

    def __init__(self) -> None:
        self._peers: dict[str, dict] = {}

    def get_peers(self) -> list[dict]:
        return list(self._peers.values())

    # zeroconf ServiceListener interface
    def add_service(self, zc, type_: str, name: str) -> None:
        try:
            info = zc.get_service_info(type_, name)
            if info is None:
                return

            # Parse TXT records
            txt = {}
            if info.properties:
                for k, v in info.properties.items():
                    key = k.decode("utf-8") if isinstance(k, bytes) else k
                    val = v.decode("utf-8") if isinstance(v, bytes) else str(v)
                    txt[key] = val

            node_id = txt.get("node_id", name)
            addresses = info.parsed_addresses()
            host = addresses[0] if addresses else "unknown"
            port = info.port

            peer = {
                "node_id": node_id,
                "url": f"http://{host}:{port}",
                "profile": txt.get("profile", "standard"),
                "version": txt.get("version", ""),
                "federation": txt.get("federation", ""),
                "transport": "mdns",
            }
            self._peers[node_id] = peer
            log.info("Discovered Axiom node via mDNS: %s at %s:%d", node_id, host, port)

        except Exception as e:
            log.debug("Could not process mDNS service %s: %s", name, e)

    def update_service(self, zc, type_: str, name: str) -> None:
        self.add_service(zc, type_, name)

    def remove_service(self, zc, type_: str, name: str) -> None:
        # Try to find and remove by name
        to_remove = [nid for nid, p in self._peers.items() if name.startswith(nid)]
        for nid in to_remove:
            del self._peers[nid]
            log.info("Axiom node removed from mDNS: %s", nid)


class MDNSService:
    """Advertises this node and discovers peers via mDNS."""

    def __init__(
        self,
        node_id: str = "",
        port: int = 8766,
        profile: str = "standard",
        version: str = "",
        federation: str = "",
    ) -> None:
        self.node_id = node_id or socket.gethostname()
        self.port = port
        self.profile = profile
        self.version = version
        self.federation = federation
        self._listener = AxiomServiceListener()
        self._zc = None
        self._info = None

    def _build_txt(self) -> dict:
        return {
            "node_id": self.node_id,
            "profile": self.profile,
            "version": self.version,
            "federation": self.federation,
        }

    def start(self) -> None:
        """Start advertising and listening for peers."""
        try:
            from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf

            self._zc = Zeroconf()

            # Advertise ourselves
            txt = {k.encode(): v.encode() for k, v in self._build_txt().items()}
            hostname = socket.gethostname()
            local_ip = _get_local_ip()

            self._info = ServiceInfo(
                SERVICE_TYPE,
                f"{self.node_id}.{SERVICE_TYPE}",
                addresses=[socket.inet_aton(local_ip)],
                port=self.port,
                properties=txt,
                server=f"{hostname}.local.",
            )
            self._zc.register_service(self._info)
            log.info("mDNS: advertising %s at %s:%d", self.node_id, local_ip, self.port)

            # Listen for other nodes
            ServiceBrowser(self._zc, SERVICE_TYPE, self._listener)
            log.info("mDNS: listening for Axiom peers on %s", SERVICE_TYPE)

        except ImportError:
            log.warning("zeroconf package not installed — mDNS discovery disabled")
        except Exception as e:
            log.warning("mDNS startup failed: %s", e)

    def stop(self) -> None:
        """Stop advertising and listening."""
        if self._zc:
            if self._info:
                self._zc.unregister_service(self._info)
            self._zc.close()
            self._zc = None

    def get_discovered_peers(self) -> list[dict]:
        """Get list of discovered peer nodes."""
        return self._listener.get_peers()


def _get_local_ip() -> str:
    """Get the local IP address (best guess for LAN/VPN)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
