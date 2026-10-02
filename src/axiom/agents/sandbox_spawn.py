# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Run one agent's heartbeat inside its own sandbox.

ADR-036 §D10 lets each agent's manifest relax the hardened defaults through
``[agent.sandbox]``. That only means something if the agent has a unit of its
own to relax. It did not: since 0.11.1 every agent is dispatched by the one
Background Service with ``subprocess.run``, so all of them share whatever
sandbox the dispatcher's unit declares. A relaxation written by one agent
would either do nothing or, applied to the dispatcher, quietly widen the
sandbox for every other agent on the slot.

Restoring per-agent granularity does NOT mean restoring per-agent persistent
units. The 2026-04-29 UX decision — one OS-level entry per installed slot,
however many agents it hosts — stands. Instead each dispatch runs in a
TRANSIENT systemd unit carrying that agent's own directives. The persistent
surface stays at one entry; the sandbox becomes per agent, which is the thing
D10 actually assumes.

The wrapping is Linux-only, and when it cannot be applied the caller is told
which kind of absence it is rather than being left to infer one. An agent
running unsandboxed because this host has no systemd is a different fact from
an agent running unsandboxed because its manifest said so, and a dispatcher
that logged the same thing for both would make the distinction unrecoverable.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping, Sequence

from axiom.infra.services import sandbox_directives

#: Wrapped: the command runs in a transient unit carrying its own directives.
APPLIED = "systemd-transient"

#: Not wrapped, and why. Each is a DIFFERENT fact about the same outcome.
NOT_LINUX = "unsandboxed:not-linux"
NO_SYSTEMD_RUN = "unsandboxed:no-systemd-run"
NO_USER_MANAGER = "unsandboxed:no-user-manager"

#: systemd needs a user manager to talk to; without these `systemd-run --user`
#: fails at connect time rather than falling back to anything useful.
_USER_MANAGER_VARS = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


def sandbox_state(
    *,
    platform: str | None = None,
    which=shutil.which,
    env: Mapping[str, str] | None = None,
) -> str:
    """Whether this host can put a dispatch in its own transient unit.

    Split from :func:`sandbox_command` so a status surface can ask the
    question without having a command in hand — `axi agents status` reports
    the host's capability, not one dispatch's outcome.
    """
    plat = sys.platform if platform is None else platform
    environ = os.environ if env is None else env
    if not plat.startswith("linux"):
        return NOT_LINUX
    if not which("systemd-run"):
        return NO_SYSTEMD_RUN
    if not any(environ.get(v) for v in _USER_MANAGER_VARS):
        return NO_USER_MANAGER
    return APPLIED


def sandbox_command(
    cmd: Sequence[str],
    sandbox: object | None,
    *,
    description: str = "",
    platform: str | None = None,
    which=shutil.which,
    env: Mapping[str, str] | None = None,
) -> tuple[list[str], str]:
    """``(argv, state)`` — the command to run, and what sandbox it will get.

    On a host that cannot wrap, the command comes back UNCHANGED and the
    state says which kind of absence it is. Refusing to dispatch instead
    would turn a hardening feature into an outage on every macOS laptop and
    every container without a user manager.

    An invalid ``[agent.sandbox]`` block is a different matter and is NOT
    swallowed here: :func:`sandbox_directives` raises, and the caller must
    decide, because a manifest that claims a sandbox it never gets is the
    one failure this whole mechanism exists to prevent. That check runs on
    every host, including the ones that cannot apply the result.
    """
    argv = list(cmd)

    # Validate BEFORE deciding whether this host can wrap. Whether a
    # declaration is well-formed is a fact about the manifest, not about the
    # machine reading it: checking only on the hosts that can apply it meant a
    # broken `[agent.sandbox]` sailed through every developer's Mac and failed
    # first on a Linux node, which is the furthest possible place from the
    # person who wrote it.
    properties = sandbox_directives(sandbox, subject=description or "agent")

    state = sandbox_state(platform=platform, which=which, env=env)
    if state != APPLIED:
        return argv, state

    run = [
        "systemd-run",
        "--user",
        # --wait exits with the command's own status, so the dispatcher keeps
        # reading a heartbeat's return code the way it always has. --collect
        # reaps the transient unit even when it fails, or a failed heartbeat
        # would leave a unit behind that blocks the next one by name.
        "--wait",
        "--collect",
        "--quiet",
        # Inherit stdio: without it the heartbeat's output goes only to the
        # journal, and every existing caller that reads it sees silence.
        "--pipe",
    ]
    if description:
        run.append(f"--description={description}")
    # Deliberately unnamed. A stable --unit name collides with a previous
    # dispatch that has not finished, which turns a slow heartbeat into a
    # failed one; the description is what makes it findable in the journal.
    for key, value in properties:
        run.append(f"--property={key}={value}")
    run.append("--")
    return run + argv, state


__all__ = [
    "APPLIED",
    "NOT_LINUX",
    "NO_SYSTEMD_RUN",
    "NO_USER_MANAGER",
    "sandbox_command",
    "sandbox_state",
]
