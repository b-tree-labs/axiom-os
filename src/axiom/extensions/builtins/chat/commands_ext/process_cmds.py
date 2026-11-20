# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/ps`` and ``/stop``: the processes THIS session started (see ``process_commands``).

Registered here so the registry stays the one place a command is declared; the logic lives with the
process-containment code it wraps.
"""

from __future__ import annotations

from ..command_registry import command


@command(
    "/ps",
    "List processes this session started (read-only)",
    "Agent",
    risk="read",
)
def ps_command(ctx, args):
    from ..process_commands import cmd_ps

    return cmd_ps(ctx.agent)


@command(
    "/stop",
    "Stop a process this session started (/stop <id>; see /ps)",
    "Agent",
    usage="/stop <id>",
    risk="destructive",
    mutates=True,
)
def stop_command(ctx, args):
    from ..process_commands import cmd_stop

    return cmd_stop(ctx.agent, args)
