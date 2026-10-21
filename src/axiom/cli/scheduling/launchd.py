# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`LaunchdBackend` — macOS scheduler impl via launchctl (issue #205, slice 8).

Apple deprecated cron on macOS; user-scope jobs live as `.plist` files
under `~/Library/LaunchAgents/` and are managed via
`launchctl bootstrap gui/$UID <plist>` / `launchctl bootout gui/$UID/<label>`.

Lifecycle:

  install:
    1. resolve LaunchAgents dir (honor AXIOM_LAUNCH_AGENTS_DIR) and $UID
    2. `launchctl bootout gui/$UID/<label>`  (tolerated if not loaded)
    3. write rendered plist to <dir>/<label>.plist
    4. `launchctl bootstrap gui/$UID <plist>`

  uninstall:
    1. resolve dir + uid
    2. `launchctl bootout gui/$UID/<label>`  (tolerated)
    3. `rm -f <plist>` (idempotent)

Cron expressions translate to launchd's StartCalendarInterval (exact
values) or StartInterval (minute steps). Ranges / lists / non-minute
steps raise — they aren't expressible in launchd's grammar, and silent
nearest-neighbor mapping would be a footgun.
"""

from __future__ import annotations

import xml.sax.saxutils as _xml
from datetime import UTC, datetime

from .protocols import RemoteRunner

LABEL_PREFIX = "com.axiom.schedule."

# Cron expressions launchd cannot express verbatim. We raise rather than
# silently round, so the user knows the schedule shape needs to change.
_BOOTOUT_TOLERATED_PATTERNS = (
    "could not find specified service",
    "could not find service",
    "no such process",
    "service is not loaded",
)


def _label_for(schedule_name: str) -> str:
    return f"{LABEL_PREFIX}{schedule_name}"


# ---------------------------------------------------------------------------
# Cron parsing → launchd trigger spec
# ---------------------------------------------------------------------------


def _parse_cron_to_triggers(cron: str) -> dict:
    """Translate a cron expression to a `{ "StartInterval": int }` or
    `{ "StartCalendarInterval": {<field>: int, ...} }` shape suitable
    for plist emission.

    Supports exact-value fields and a minute-step `*/N` shortcut. Ranges
    (`0-30`), lists (`0,15,30`), and step values on non-minute fields
    raise ValueError — launchd has no faithful encoding for them.
    """
    cron = cron.strip()

    macro_map = {
        "@hourly": "0 * * * *",
        "@daily": "0 0 * * *",
        "@midnight": "0 0 * * *",
        "@weekly": "0 0 * * 0",
        "@monthly": "0 0 1 * *",
        "@yearly": "0 0 1 1 *",
        "@annually": "0 0 1 1 *",
    }
    if cron in macro_map:
        cron = macro_map[cron]
    elif cron.startswith("@"):
        raise ValueError(f"unsupported cron macro for launchd: {cron!r}")

    fields = cron.split()
    if len(fields) != 5:
        raise ValueError(
            f"cron expression must have 5 fields, got {len(fields)}: {cron!r}"
        )
    minute, hour, day, month, weekday = fields

    # Minute step shortcut → StartInterval (seconds)
    if minute.startswith("*/") and hour == "*" and day == "*" and month == "*" and weekday == "*":
        try:
            step = int(minute[2:])
        except ValueError as exc:
            raise ValueError(f"invalid minute step {minute!r}") from exc
        if step <= 0:
            raise ValueError(f"minute step must be positive: {minute!r}")
        return {"StartInterval": step * 60}

    # Build StartCalendarInterval dict from exact-valued fields.
    name_map = (
        ("Minute", minute),
        ("Hour", hour),
        ("Day", day),
        ("Month", month),
        ("Weekday", weekday),
    )
    interval: dict[str, int] = {}
    for name, raw in name_map:
        if raw == "*":
            continue
        if "-" in raw:
            raise ValueError(
                f"cron range expressions aren't expressible in launchd: "
                f"{name}={raw!r}"
            )
        if "," in raw:
            raise ValueError(
                f"cron list expressions aren't expressible in launchd: "
                f"{name}={raw!r}"
            )
        if raw.startswith("*/") or "/" in raw:
            raise ValueError(
                f"cron step expressions on {name} aren't expressible in "
                f"launchd: {raw!r}"
            )
        try:
            interval[name] = int(raw)
        except ValueError as exc:
            raise ValueError(
                f"unparsable cron value for {name}: {raw!r}"
            ) from exc

    return {"StartCalendarInterval": interval}


# ---------------------------------------------------------------------------
# Plist emission
# ---------------------------------------------------------------------------


def _xml_escape(s: str) -> str:
    return _xml.escape(s)


def _emit_plist(*, label: str, triggers: dict, command: str, generated_at: str) -> str:
    """Hand-rolled plist emitter — no plistlib dep, easier to assert on
    in tests."""
    body_lines: list[str] = []
    body_lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    body_lines.append(
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    )
    body_lines.append('<plist version="1.0">')
    body_lines.append('<dict>')
    body_lines.append(f'  <!-- Generated by `axi schedule` on {generated_at} -->')
    body_lines.append('  <key>Label</key>')
    body_lines.append(f'  <string>{_xml_escape(label)}</string>')
    body_lines.append('  <key>ProgramArguments</key>')
    body_lines.append('  <array>')
    body_lines.append('    <string>/bin/sh</string>')
    body_lines.append('    <string>-c</string>')
    body_lines.append(f'    <string>{_xml_escape(command)}</string>')
    body_lines.append('  </array>')

    if "StartInterval" in triggers:
        body_lines.append('  <key>StartInterval</key>')
        body_lines.append(f'  <integer>{triggers["StartInterval"]}</integer>')
    else:
        interval = triggers["StartCalendarInterval"]
        body_lines.append('  <key>StartCalendarInterval</key>')
        body_lines.append('  <dict>')
        for key, val in interval.items():
            body_lines.append(f'    <key>{key}</key>')
            body_lines.append(f'    <integer>{val}</integer>')
        body_lines.append('  </dict>')

    body_lines.append('  <key>RunAtLoad</key>')
    body_lines.append('  <false/>')
    body_lines.append('</dict>')
    body_lines.append('</plist>')
    return "\n".join(body_lines) + "\n"


# ---------------------------------------------------------------------------
# Runner helpers
# ---------------------------------------------------------------------------


def _resolve_agents_dir(runner: RemoteRunner) -> str:
    """Resolve `${AXIOM_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}` on
    the runner.

    Passed as a *string* (not argv) so SSHRunner sends it to the remote
    shell unchanged; argv form gets re-flattened by ssh and breaks the
    `sh -c '...'` quoting.
    """
    result = runner.run(
        'echo "${AXIOM_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"'
    )
    if not result.ok:
        raise RuntimeError(
            f"could not resolve LaunchAgents dir on {runner.host!r}: "
            f"rc={result.returncode} stderr={result.stderr!r}"
        )
    return result.stdout.strip()


def _resolve_uid(runner: RemoteRunner) -> str:
    result = runner.run(["id", "-u"])
    if not result.ok:
        raise RuntimeError(
            f"could not resolve uid on {runner.host!r}: "
            f"rc={result.returncode} stderr={result.stderr!r}"
        )
    return result.stdout.strip()


def _is_tolerated_bootout_error(stderr: str) -> bool:
    s = stderr.lower()
    return any(p in s for p in _BOOTOUT_TOLERATED_PATTERNS)


def _existing_plist_matches(
    runner: RemoteRunner, plist_path: str, expected: str,
) -> bool:
    """True iff the on-host plist exists and its content equals `expected`.

    Falsy on read error (missing file, permission denied, etc.) so the
    caller proceeds with the full install path. Defensive — we'd rather
    re-install than skip when uncertain.

    Passed as a shell-string command (not argv) so SSHRunner forwards
    it verbatim to the remote shell rather than re-flattening argv —
    same pattern as `_resolve_agents_dir`.
    """
    import shlex
    result = runner.run(f"cat {shlex.quote(plist_path)} 2>/dev/null")
    return result.ok and result.stdout == expected


def _is_loaded(runner: RemoteRunner, *, uid: str, label: str) -> bool:
    """True iff `launchctl list <label>` exits 0 (loaded into gui/$UID)."""
    result = runner.run(["launchctl", "list", label])
    return result.ok


def _bootout(runner: RemoteRunner, *, uid: str, label: str) -> None:
    target = f"gui/{uid}/{label}"
    result = runner.run(["launchctl", "bootout", target])
    if result.ok:
        return
    if _is_tolerated_bootout_error(result.stderr):
        return
    # Some launchd versions return non-zero with empty stderr for
    # not-loaded jobs. Be lenient — bootstrap is the real test.
    if not result.stderr.strip():
        return
    raise RuntimeError(
        f"launchctl bootout {target!r} failed on {runner.host!r}: "
        f"rc={result.returncode} stderr={result.stderr!r}"
    )


def _bootstrap(runner: RemoteRunner, *, uid: str, plist_path: str) -> None:
    result = runner.run(["launchctl", "bootstrap", f"gui/{uid}", plist_path])
    if not result.ok:
        raise RuntimeError(
            f"launchctl bootstrap of {plist_path!r} failed on "
            f"{runner.host!r}: rc={result.returncode} "
            f"stderr={result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class LaunchdBackend:
    """SchedulerBackend impl for macOS launchd."""

    name: str = "launchd"

    def artifact_filename(self, schedule_name: str) -> str:
        return f"{schedule_name}.plist"

    def render(self, *, schedule_name: str, cron: str, command: str) -> str:
        triggers = _parse_cron_to_triggers(cron)
        label = _label_for(schedule_name)
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        return _emit_plist(
            label=label, triggers=triggers, command=command, generated_at=timestamp,
        )

    def install_artifact(
        self,
        *,
        runner: RemoteRunner,
        schedule_name: str,
        artifact_content: str,
    ) -> None:
        """The artifact is already a complete plist — no parsing needed.

        Idempotence: if the on-disk plist already matches `artifact_content`
        AND the agent is already loaded, skip the whole bootout+write+
        bootstrap cycle. macOS surfaces the Login Items toast on every
        write+load to ~/Library/LaunchAgents/; no-op skips silence the
        toast on repeated `axi schedule install` runs (issue #208).
        """
        agents_dir = _resolve_agents_dir(runner)
        uid = _resolve_uid(runner)
        label = _label_for(schedule_name)
        plist_path = f"{agents_dir.rstrip('/')}/{label}.plist"

        if _existing_plist_matches(runner, plist_path, artifact_content) \
                and _is_loaded(runner, uid=uid, label=label):
            return

        _bootout(runner, uid=uid, label=label)
        runner.write_file(plist_path, artifact_content)
        _bootstrap(runner, uid=uid, plist_path=plist_path)

    def install(
        self,
        *,
        runner: RemoteRunner,
        schedule_name: str,
        cron: str,
        command: str,
    ) -> None:
        artifact = self.render(
            schedule_name=schedule_name, cron=cron, command=command,
        )
        self.install_artifact(
            runner=runner,
            schedule_name=schedule_name,
            artifact_content=artifact,
        )

    def uninstall(
        self,
        *,
        runner: RemoteRunner,
        schedule_name: str,
    ) -> None:
        agents_dir = _resolve_agents_dir(runner)
        uid = _resolve_uid(runner)
        label = _label_for(schedule_name)
        plist_path = f"{agents_dir.rstrip('/')}/{label}.plist"

        _bootout(runner, uid=uid, label=label)
        # `rm -f` is idempotent (no error on missing file).
        runner.run(["rm", "-f", plist_path])
