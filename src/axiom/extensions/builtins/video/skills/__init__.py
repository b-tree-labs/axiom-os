# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The video skills, declared once: ``video.add``, ``video.verify``, ``video.list``,
``video.status``, ``video.start``, ``video.stop``, ``video.diagnose``, ``video.install``,
``video.uninstall``, ``video.revoke``, ``video.events`` (ADR-056).

Every ``<cli> instrument video`` verb is a thin wrapper over one of these, through
``invoke_capability``, so the command line, MCP and an agent reach the same
function and get the same answer. The descriptions here are what an agent reads,
and ``axi skills emit-md --ext video`` turns them into each skill's SKILL.md and
the manifest's skill blocks, so there is one source.

Reads (list, status, diagnose, events) reach MCP and agents. The verbs that change
a node's capture stay on its own command line. A trigger is attached by
configuration and ``connector reload``, so there is no verb for it (VID-015).
"""

from __future__ import annotations

from axiom.infra.skill_declarations import NextStep
from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from . import add as _add
from . import diagnose as _diagnose
from . import events as _events
from . import install as _install
from . import listing as _listing
from . import revoke as _revoke
from . import start as _start
from . import status as _status
from . import stop as _stop
from . import uninstall as _uninstall
from . import verify as _verify
from ._checks import FFMPEG_AVAILABLE, NUMPY_AVAILABLE, SOURCE_DECLARED, SOURCE_VERIFIED

_READS = ("cli", "mcp", "agent_tool")
_WRITES = ("cli",)

_DIAGNOSE_ON_ERROR = NextStep(
    "video.diagnose", "error", "find the rung that fails and its exact fix"
)

SPECS = (
    SkillSpec(
        name="video.add",
        fn=_add.run,
        description=(
            "Declare a video source (a connector of kind video) and verify it. Writes the connector file "
            "with the source kind's parameters (for rtsp: url and transport) and a credential_ref into the "
            "vault, then asks the source for one frame and records codec, size, frame rate and latency. A "
            "URL that carries a username or password is refused with the vault instruction. The source is "
            "declared even when verify fails, and is not active until a verify passes."
        ),
        inputs={
            "name": "the source's name (lowercase letters, digits, - and _)",
            "kind": "the source kind, e.g. rtsp (`video.list` with kinds=true lists them)",
            "credential_ref": "optional secret ref for the login, e.g. foreign://video-cam-1",
            "fps_cap": "decoded frames per second offered to the bus (default 10)",
            "source": "the kind's parameters, e.g. {url, transport}; or pass them as top-level params",
        },
        surfaces=_WRITES,
        side_effects=True,
        idempotent=False,
        open_world=True,
        risk="reversible-write",
        requires=(),
        next_steps=(
            NextStep("video.start", "ok", "start capturing the verified source"),
            _DIAGNOSE_ON_ERROR,
        ),
    ),
    SkillSpec(
        name="video.verify",
        fn=_verify.run,
        description=(
            "Ask a declared video source for one frame through the same ffmpeg pipe capture uses, and "
            "record what it showed: codec, width, height, fps and latency_ms. A pass makes the source "
            "active; a failure records the classified error and its fix (login refused, not found, "
            "unreachable, codec, ffmpeg missing) with no credential in it."
        ),
        inputs={"name": "the declared source"},
        surfaces=_WRITES,
        side_effects=True,
        idempotent=True,
        open_world=True,
        risk="reversible-write",
        requires=(SOURCE_DECLARED,),
        next_steps=(
            NextStep("video.start", "ok", "start capturing it"),
            _DIAGNOSE_ON_ERROR,
        ),
    ),
    SkillSpec(
        name="video.list",
        fn=_listing.run,
        description=(
            "List the declared video sources with their kind, health and capture state; with kinds=true, "
            "the registered source kinds and whether each is replayable or live; with triggers=true, the "
            "triggers that can be attached to a source (name, version, where each comes from, what it "
            "does) and any that were refused or declared twice, with the reason. Reads only."
        ),
        inputs={
            "kinds": "list the registered source kinds instead of the sources",
            "triggers": "list the triggers that can be attached instead of the sources",
        },
        surfaces=_READS,
        side_effects=False,
        idempotent=True,
        open_world=False,
        risk="read",
        requires=(),
        next_steps=(NextStep("video.status", "always", "see each source's session and counters"),),
    ),
    SkillSpec(
        name="video.status",
        fn=_status.run,
        description=(
            "Every video source's capture as it is now: health (ready, degraded, failed, unmounted), "
            "session state, the process holding it, session_id, fps, frames, drops, reconnects and "
            "last_frame_age_s, the last verify, and any error with its fix. Credentials are never shown, "
            "only the reference to them. Answers 'is the camera alive', 'is it still streaming'. Reads only."
        ),
        inputs={"name": "optional: one source"},
        surfaces=_READS,
        side_effects=False,
        idempotent=True,
        open_world=False,
        risk="read",
        requires=(),
        next_steps=(
            NextStep("video.diagnose", "always", "find out why a source is degraded or failed"),
        ),
    ),
    SkillSpec(
        name="video.start",
        fn=_start.run,
        description=(
            "Start capturing a verified video source: mark it started and bring up a capture service that "
            "takes the source's claim, holds one session on it and reconnects when it is lost. A source is "
            "captured whether or not anything consumes its frames. Starting a source that a live copy "
            "already holds is a no-op that says so."
        ),
        inputs={"name": "the source to capture"},
        surfaces=_WRITES,
        side_effects=True,
        idempotent=True,
        open_world=True,
        risk="reversible-write",
        requires=(SOURCE_DECLARED, SOURCE_VERIFIED, FFMPEG_AVAILABLE, NUMPY_AVAILABLE),
        next_steps=(
            NextStep("video.status", "ok", "watch its counters advance"),
            _DIAGNOSE_ON_ERROR,
        ),
    ),
    SkillSpec(
        name="video.stop",
        fn=_stop.run,
        description=(
            "Stop capturing a video source: clear its started mark; the copy holding it kills its decoder "
            "and releases the claim within the drain time. Stopping a source that is not running says so."
        ),
        inputs={"name": "the source to stop"},
        surfaces=_WRITES,
        side_effects=True,
        idempotent=True,
        open_world=False,
        risk="reversible-write",
        requires=(),
        next_steps=(NextStep("video.status", "ok", "confirm nothing holds it"),),
    ),
    SkillSpec(
        name="video.install",
        fn=_install.run,
        description=(
            "Make video capture come back after a reboot: register a capture service that captures every "
            "started source as a unit this machine's service manager starts at boot (a systemd user unit, "
            "launchd or Task Scheduler), pinned to this install's state directory, and start it. Refused "
            "where nothing would survive a reboot. Says whether a systemd unit will outlive the login."
        ),
        inputs={},
        surfaces=_WRITES,
        side_effects=True,
        idempotent=True,
        open_world=False,
        risk="reversible-write",
        requires=(),
        next_steps=(
            NextStep("video.status", "ok", "see that the service holds the started sources"),
        ),
    ),
    SkillSpec(
        name="video.uninstall",
        fn=_uninstall.run,
        description=(
            "Stop the boot-time capture service and remove its unit. Started sources stay started; after this "
            "they are captured only by `instrument video start` until the service is installed again."
        ),
        inputs={},
        surfaces=_WRITES,
        side_effects=True,
        idempotent=True,
        open_world=False,
        risk="reversible-write",
        requires=(),
        next_steps=(
            NextStep("video.status", "ok", "see which started sources nothing is capturing now"),
        ),
    ),
    SkillSpec(
        name="video.revoke",
        fn=_revoke.run,
        description=(
            "Remove a video source: stop capturing it, release it, and delete its declaration and what was "
            "recorded about it (the last verify, its session). Its login stays in the vault, where another "
            "source may use it; the result says how to remove it. Undo by adding the source again."
        ),
        inputs={"name": "the source to remove"},
        surfaces=_WRITES,
        side_effects=True,
        idempotent=True,
        open_world=False,
        risk="reversible-write",
        requires=(SOURCE_DECLARED,),
        next_steps=(NextStep("video.list", "ok", "confirm it is gone"),),
    ),
    SkillSpec(
        name="video.events",
        fn=_events.run,
        description=(
            "What the triggers on the video sources saw, from the node's event ledger, oldest first: each "
            "event's source, trigger and version, time, kind (an episode's started and ended share one "
            "incident key; occurred is a moment), severity, score and detail. Answers 'did anything move "
            "on the door camera this morning'. Optionally one source, optionally since a time. Reads only."
        ),
        inputs={
            "name": "optional: one source (its history, even if it is no longer declared)",
            "since": "optional: only events at or after this ISO-8601 time (no offset means local time)",
        },
        surfaces=_READS,
        side_effects=False,
        idempotent=True,
        open_world=False,
        risk="read",
        requires=(),
        next_steps=(
            NextStep("video.status", "always", "see each source's triggers and their health"),
        ),
    ),
    SkillSpec(
        name="video.diagnose",
        fn=_diagnose.run,
        description=(
            "Find out why a video source does not work, as a ladder whose first failing rung names the exact "
            "fix: ffmpeg installed, numpy installed, the source declared, its kind registered, its "
            "credential resolvable, then the kind's own rungs (for rtsp: the URL resolves, DESCRIBE answers, "
            "the login is accepted, the codec is decodable), then one real frame. Contacts the source; "
            "changes nothing."
        ),
        inputs={"name": "the source to diagnose"},
        surfaces=_READS,
        side_effects=False,
        idempotent=True,
        open_world=True,
        risk="read",
        requires=(),
        next_steps=(
            NextStep("video.verify", "ok", "record a passing verify so the source is active"),
        ),
    ),
)


def bind(registry: SkillRegistry) -> list[str]:
    names = []
    for spec in SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=spec.side_effects is True)
        names.append(spec.name)
    return names


def bind_default() -> SkillRegistry:
    """Bind into the process-default registry (what ``axi skills emit-md`` and MCP read)."""
    registry = default_registry()
    bind(registry)
    return registry


__all__ = ["SPECS", "bind", "bind_default"]
