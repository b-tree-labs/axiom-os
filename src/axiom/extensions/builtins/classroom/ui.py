# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Student/instructor-facing output helpers for the classroom ceremony.

Centralizes what the three ceremony commands (``invite``, ``serve``,
``join``) and the ``status`` dashboard emit to the terminal. Using one
module means the voice is consistent across surfaces, and the
"no-jargon" rule is enforced by editing one file instead of hunting
through handler code.

Graceful degradation: when the output stream isn't a TTY (CI, pipes,
``capsys`` in pytest), :class:`rich.console.Console` emits plain text
without ANSI codes. Regression tests that grep for literal phrases
keep working.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Consoles are rebuilt per-call so that pytest capsys (which swaps
# sys.stdout/sys.stderr during tests) sees the output. Construction cost
# is small and these commands are not hot paths.
#
# Width is fixed at 200 only when the stream is NOT a terminal — that's
# pytest's capsys (via StringIO) or a pipe. In those cases Rich would
# otherwise default to 80 columns and wrap our long invite tokens across
# lines, breaking `re.search(\S+)` in regression tests and making email
# forwards ugly. When stdout IS a terminal, we let Rich use the real
# terminal width so panels match the window.


def _width_for(stream) -> int | None:
    try:
        if stream.isatty():
            return None  # let Rich detect terminal width
    except (AttributeError, ValueError):
        pass
    return 200


def out() -> Console:
    """Return a stdout Console bound to the CURRENT sys.stdout."""
    return Console(
        file=sys.stdout,
        highlight=False,
        soft_wrap=True,
        width=_width_for(sys.stdout),
    )


def err() -> Console:
    """Return a stderr Console bound to the CURRENT sys.stderr."""
    return Console(
        file=sys.stderr,
        highlight=False,
        soft_wrap=True,
        width=_width_for(sys.stderr),
    )


# ---------------------------------------------------------------------------
# Invite command
# ---------------------------------------------------------------------------


def emit_invite_ready(
    classroom_id: str,
    encoded: str,
    expiry_friendly: str,
) -> None:
    """Copy-paste-ready invite block for the instructor to forward.

    The join command (with its long encoded token) is emitted as a
    plain line — no Rich wrapping — so email clients and humans can
    copy it verbatim.
    """
    console = out()
    console.print()
    console.print(f'Invite ready for class "[bold]{classroom_id}[/]".')
    console.print()
    console.print(
        "[dim]Send the line below to your student "
        "(email, chat — anywhere):[/]"
    )
    console.print()
    # Plain print for the command: no decoration, no wrapping, exactly
    # what the instructor forwards.
    sys.stdout.write(f"    axi classroom join {encoded}\n")
    sys.stdout.flush()
    console.print()
    console.print(f"[dim]Expires {expiry_friendly}.[/]")


def emit_need_coordinator_url(classroom_id: str) -> None:
    """First-run error: no URL stored and no flag passed."""
    msg = Text()
    msg.append(
        "This is your first invite for this class. I need to know "
        "where your classroom server is reachable — pass it once and "
        "I'll remember it:\n\n",
    )
    msg.append(
        f"  axi classroom invite {classroom_id} "
        "--coordinator-url https://your-server.example/classroom/join\n",
        style="cyan",
    )
    err().print(msg)


# ---------------------------------------------------------------------------
# Serve command
# ---------------------------------------------------------------------------


def emit_serve_banner(
    classroom_id: str,
    local_url: str,
    public_url: str | None = None,
) -> None:
    """Startup banner for `axi classroom serve`."""
    body = Text()
    body.append("Listening on: ", style="dim")
    body.append(local_url, style="cyan")
    if public_url and public_url != local_url:
        body.append("\nStudents connect via: ", style="dim")
        body.append(public_url, style="cyan")

    panel = Panel(
        body,
        title=f'Class "{classroom_id}" is open',
        subtitle="Ctrl-C to stop",
        border_style="green",
        padding=(1, 2),
    )
    out().print()
    out().print(panel)
    out().print()
    out().print(
        f"[dim]Create invites in another terminal:[/] "
        f"[cyan]axi classroom invite {classroom_id}[/]"
    )
    out().print()


def emit_serve_needs_bootstrap(classroom_id: str) -> None:
    """No cohort set up yet — point the instructor at exactly the command to run."""
    msg = Text()
    msg.append(f'No class "{classroom_id}" is set up on this machine yet.\n\n')
    msg.append("Create your first invite to get started:\n")
    msg.append(
        f"  axi classroom invite {classroom_id} "
        "--coordinator-url https://your-server.example/classroom/join\n",
        style="cyan",
    )
    err().print(msg)


def emit_student_joined(student_id: str) -> None:
    """Live one-liner printed to the serve terminal when a student joins."""
    out().print(f"  [green]✓[/] {student_id} joined")


def emit_serve_stopping() -> None:
    out().print("\n[dim]Stopping class server. See you next time![/]")


# ---------------------------------------------------------------------------
# Join command
# ---------------------------------------------------------------------------


@contextmanager
def joining_spinner(classroom_id: str) -> Iterator[None]:
    """Spinner while the student's join request is in flight."""
    # Fall back to plain text when stderr isn't a TTY so pytest capture
    # doesn't see spinner control sequences.
    if not sys.stderr.isatty():
        err().print(f"Joining class \"{classroom_id}\"...")
        yield
        return

    with err().status(
        f'Joining class "{classroom_id}"...',
        spinner="dots",
    ):
        yield


def emit_join_success(classroom_id: str, student_id: str) -> None:
    """Celebratory landing when a student successfully joins."""
    body = Text()
    body.append("You're in! ", style="bold green")
    body.append("Hi ", style="")
    body.append(student_id, style="bold")
    body.append(", welcome to ")
    body.append(classroom_id, style="bold")
    body.append(".")

    out().print()
    out().print(Panel(body, border_style="green", padding=(1, 2)))


def emit_identity_autoinit(owner: str) -> None:
    """One-line narration when the student's identity is auto-generated."""
    err().print(
        f"[dim]First time here — setting up your identity as "
        f"[/][bold]{owner}[/][dim]. (One-time setup.)[/]"
    )


def emit_invite_damaged() -> None:
    err().print(
        "That invite looks [red]damaged[/] (copy-paste issue?). "
        "Try copying the full invite from your instructor's message again."
    )


# ---------------------------------------------------------------------------
# Status dashboard
# ---------------------------------------------------------------------------


def emit_status_empty() -> None:
    out().print(
        "[dim]No classes are set up on this machine yet. "
        "Create one with:[/] [cyan]axi classroom invite <classroom_id> "
        "--coordinator-url URL[/]"
    )


def emit_status_cohort_list(rows: list[dict]) -> None:
    """Instructor-facing table: one row per classroom on this machine."""
    if not rows:
        emit_status_empty()
        return

    table = Table(
        title="Your classes",
        title_style="bold",
        border_style="dim",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Class")
    table.add_column("Students", justify="right")
    table.add_column("Server URL", style="cyan")

    for row in rows:
        member_count = str(row["member_count"])
        url = row["coordinator_url"] or "[dim]not set[/]"
        table.add_row(row["classroom_id"], member_count, url)

    out().print(table)


def emit_status_cohort_detail(
    classroom_id: str,
    coordinator_url: str | None,
    members: list[dict],
    pending_invites: int,
) -> None:
    """Drill-down on one cohort: members, invite liveness, next actions."""
    header = Text()
    header.append(classroom_id, style="bold")
    header.append(" · ", style="dim")
    header.append(f"{len(members)} student{'' if len(members) == 1 else 's'}")
    if coordinator_url:
        header.append(" · ", style="dim")
        header.append(coordinator_url, style="cyan")

    out().print()
    out().print(header)
    out().print()

    if members:
        table = Table(
            border_style="dim",
            show_header=True,
            header_style="bold",
        )
        table.add_column("Student")
        table.add_column("Status")
        table.add_column("Joined")
        for m in members:
            status = m["status"]
            style = {"ACTIVE": "green", "QUARANTINED": "yellow", "REVOKED": "red"}.get(
                status, ""
            )
            table.add_row(
                m["student_id"],
                f"[{style}]{status}[/]" if style else status,
                m.get("joined_at") or "",
            )
        out().print(table)
    else:
        out().print("[dim]No students have joined yet.[/]")

    if pending_invites > 0:
        out().print()
        out().print(
            f"[dim]{pending_invites} invite{'' if pending_invites == 1 else 's'} "
            f"outstanding.[/]"
        )


# ---------------------------------------------------------------------------
# Generic helpers — the palette most classroom commands reach for
# ---------------------------------------------------------------------------


def emit_success(message: str) -> None:
    """One-line green checkmark + message."""
    out().print(f"[green]✓[/] {message}")


def emit_info(message: str) -> None:
    """One-line dim informational message."""
    out().print(f"[dim]{message}[/]")


def emit_warn(message: str) -> None:
    out().print(f"[yellow]![/] {message}")


def emit_error(message: str) -> None:
    err().print(f"[red]✗[/] {message}")


def emit_next_steps(steps: list[str], title: str = "Next") -> None:
    """Show a short list of suggested next commands for the user."""
    if not steps:
        return
    out().print()
    out().print(f"[dim]{title}:[/]")
    for step in steps:
        out().print(f"  [cyan]{step}[/]")


def emit_kv(title: str, items: dict) -> None:
    """Title + indented key:value lines."""
    out().print()
    out().print(f"[bold]{title}[/]")
    for k, v in items.items():
        out().print(f"  [dim]{k}:[/] {v}")


def emit_rule(title: str = "") -> None:
    """Horizontal rule with optional caption."""
    from rich.rule import Rule
    out().print(Rule(title=title, style="dim"))


def emit_table(
    *,
    title: str | None,
    columns: list[str],
    rows: list[list[str]],
    column_styles: list[str] | None = None,
) -> None:
    """Generic Rich table with sensible defaults.

    ``column_styles`` applies a style per column in order; pass "" to skip.
    """
    table = Table(
        title=title,
        title_style="bold" if title else None,
        border_style="dim",
        show_header=True,
        header_style="bold",
    )
    styles = column_styles or [""] * len(columns)
    for col, style in zip(columns, styles):
        table.add_column(col, style=style or None)
    for row in rows:
        table.add_row(*row)
    out().print(table)


# ---------------------------------------------------------------------------
# Workflow checklist — what `prep init`, `prep status`, etc. show
# ---------------------------------------------------------------------------


_STATUS_STYLE = {
    "completed": ("green", "✓"),
    "failed": ("red", "✗"),
    "warning": ("yellow", "!"),
    "pending": ("dim", "·"),
}


def emit_checklist(
    *,
    title: str,
    subtitle: str | None = None,
    steps: list,  # list of objects with .name, .status, .message, .critical
    ready: bool,
    ready_message: str | None = None,
    blockers: list[str] | None = None,
    next_command: str | None = None,
) -> None:
    """Render a workflow-checklist view.

    Used by the prep-flow commands to show "here's where your course /
    classroom is" without making the instructor parse ASCII art.
    """
    console = out()
    console.print()
    console.print(f"[bold]{title}[/]")
    if subtitle:
        console.print(f"[dim]{subtitle}[/]")
    console.print()

    table = Table(
        show_header=False,
        show_lines=False,
        border_style="dim",
        box=None,
        padding=(0, 1),
    )
    table.add_column()  # glyph
    table.add_column()  # crit/opt
    table.add_column(overflow="fold")  # name
    table.add_column(overflow="fold")  # status + message

    for step in steps:
        color, glyph = _STATUS_STYLE.get(step.status, ("", "?"))
        flag = "[red]required[/]" if step.critical else "[dim]optional[/]"
        msg = f" — {step.message}" if step.message else ""
        name = f"[bold]{step.name}[/]"
        status = f"[{color}]{step.status}{msg}[/]" if color else f"{step.status}{msg}"
        table.add_row(f"[{color}]{glyph}[/]" if color else glyph, flag, name, status)

    console.print(table)
    console.print()

    if ready:
        msg = ready_message or "Ready."
        console.print(f"[green]✓[/] {msg}")
        if next_command:
            console.print(f"  [cyan]{next_command}[/]")
    else:
        blockers = blockers or []
        count = len(blockers)
        noun = "blocker" if count == 1 else "blockers"
        console.print(f"[yellow]Not ready yet — {count} {noun}:[/]")
        for b in blockers:
            console.print(f"  [dim]•[/] {b}")


# ---------------------------------------------------------------------------
# Error banner + "you'll want to do X next" pairs, for commands that
# sometimes succeed with a caveat
# ---------------------------------------------------------------------------


def emit_blocked(
    *,
    what: str,
    blockers: list[str],
    suggestion: str | None = None,
) -> None:
    """Framed block: couldn't do X because Y — then maybe a next step."""
    console = err()
    console.print()
    console.print(f"[red]Couldn't {what}:[/]")
    for b in blockers:
        console.print(f"  [dim]•[/] {b}")
    if suggestion:
        console.print()
        console.print(f"[dim]Try:[/] [cyan]{suggestion}[/]")


__all__ = [
    "emit_blocked",
    "emit_checklist",
    "emit_error",
    "emit_identity_autoinit",
    "emit_info",
    "emit_invite_damaged",
    "emit_invite_ready",
    "emit_join_success",
    "emit_kv",
    "emit_need_coordinator_url",
    "emit_next_steps",
    "emit_rule",
    "emit_serve_banner",
    "emit_serve_needs_bootstrap",
    "emit_serve_stopping",
    "emit_status_cohort_detail",
    "emit_status_cohort_list",
    "emit_status_empty",
    "emit_student_joined",
    "emit_success",
    "emit_table",
    "emit_warn",
    "err",
    "joining_spinner",
    "out",
]
