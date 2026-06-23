# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Laptop presence + read-only pull (ADR-074, A3).

A hosted agent (on a self-hosted node) needs to know whether its owner's laptop is online,
and — to the degree permitted — read declared signals/files from it while it is.
Presence reuses the federation node registry (``KnownNode``/``NodeState``);
reads run through the governed ``host_exec`` seam against a per-principal
*declared* allowlist (the owner publishes what may be read).

Read-only now. The seam is positioned for later gated command-exec without
rework: every host call goes through ``_run`` with a ``write`` flag and an
optional ``policy_gate``; today only ``write=False`` (read) calls happen and the
gate is a no-op. Wiring gated exec later means passing ``write=True`` and a gate
backed by ``Ownership.can_exercise`` — no structural change here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from axiom.infra.host_exec import ExecResult, HostExecutor, HostTarget, executor_for
from axiom.vega.federation.discovery import NodeRegistry, NodeState

# policy_gate(write: bool) -> bool — return True to allow. None = allow (read-only floor).
PolicyGate = Callable[[bool], bool]

# NodeState is a trust-lifecycle enum, not a reachability flag. A laptop is
# "online" when a reachability prober has placed it in a reached-and-usable
# state; UNREACHABLE/UNKNOWN/LEAVING/EVICTED = offline.
_ONLINE_STATES = frozenset(
    {NodeState.DISCOVERED, NodeState.VERIFIED, NodeState.TRUSTED, NodeState.FEDERATED}
)


class LaptopOffline(RuntimeError):
    """Raised when a read is attempted but the laptop is not reachable."""


class NotDeclared(PermissionError):
    """Raised when a signal/file isn't on the owner's declared allowlist."""


@dataclass
class LaptopBridge:
    """Presence + read-only pull for one owner's laptop node."""

    node_id: str
    registry: NodeRegistry
    # The owner-published allowlist: signal name → read-only argv; and readable file paths.
    declared_signals: dict[str, list[str]] = field(default_factory=dict)
    declared_files: frozenset[str] = frozenset()
    executor: HostExecutor | None = None       # injected in tests; else built from the node
    policy_gate: PolicyGate | None = None       # no-op today; future gated-exec hook

    # --- presence -----------------------------------------------------------
    def _node(self):
        return self.registry.get(self.node_id)

    def is_online(self) -> bool:
        node = self._node()
        return node is not None and node.state in _ONLINE_STATES

    def last_seen(self) -> str:
        node = self._node()
        return node.last_seen if node else ""

    # --- read-only pull -----------------------------------------------------
    def read_declared_file(self, path: str) -> str:
        if path not in self.declared_files:
            raise NotDeclared(f"{path!r} is not on the laptop's declared read allowlist")
        return self._run(["cat", path], write=False).stdout

    def read_declared_signal(self, name: str) -> str:
        argv = self.declared_signals.get(name)
        if argv is None:
            raise NotDeclared(f"signal {name!r} is not declared")
        return self._run(list(argv), write=False).stdout

    # --- governed host call (the forward-compat seam) -----------------------
    def _run(self, argv: list[str], *, write: bool) -> ExecResult:
        if self.policy_gate is not None and not self.policy_gate(write):
            raise PermissionError(f"policy denied {'write' if write else 'read'} host call")
        if not self.is_online():
            raise LaptopOffline(f"laptop {self.node_id} is offline")
        return self._executor().run(argv)

    def _executor(self) -> HostExecutor:
        if self.executor is not None:
            return self.executor
        node = self._node()
        if node is None or not node.ssh_host:
            raise LaptopOffline(f"no ssh route to {self.node_id}")
        return executor_for(HostTarget(name=self.node_id, kind="ssh", ssh_host=node.ssh_host))


__all__ = ["LaptopBridge", "LaptopOffline", "NotDeclared", "PolicyGate"]
