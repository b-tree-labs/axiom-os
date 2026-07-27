# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TypedDict payload schemas for the platform's closed event taxonomy.

Per ``spec-hooks.md`` §4: payload schemas are forward-compatible
(``total=False``) — new keys may appear; old keys never disappear in
the same major version. Authors who want static typing import these;
authors who want raw flexibility take ``dict[str, Any]``.

Multi-modal references (``ImageRef``, ``AudioRef``, ``FileRef``) are
shipped now so the v1 surface accommodates future media-rich payloads
without a breaking change.
"""

from __future__ import annotations

from typing import Any, TypedDict

# ---------------------------------------------------------------------------
# Multi-modal references — used inside payloads that carry attachments.
# ---------------------------------------------------------------------------


class ImageRef(TypedDict, total=False):
    """A reference to an image attached to an event payload."""

    uri: str
    inline_bytes: bytes
    media_type: str


class AudioRef(TypedDict, total=False):
    """A reference to an audio clip attached to an event payload."""

    uri: str
    inline_bytes: bytes
    media_type: str


class FileRef(TypedDict, total=False):
    """A reference to a file attached to an event payload."""

    uri: str
    inline_bytes: bytes
    media_type: str
    filename: str


# ---------------------------------------------------------------------------
# Interceptor event payloads (HookBus subjects)
# ---------------------------------------------------------------------------


class ToolPreInvokePayload(TypedDict, total=False):
    """Payload for ``tool.pre_invoke``."""

    tool_name: str
    args: dict[str, Any]
    principal: str
    classification: str
    ext_origin: str
    images: list[ImageRef]
    audio: list[AudioRef]
    files: list[FileRef]


class PromptPreSubmitPayload(TypedDict, total=False):
    """Payload for ``prompt.pre_submit``."""

    messages: list[dict[str, Any]]
    system_layers: list[dict[str, Any]]
    principal: str
    model_id: str
    images: list[ImageRef]
    audio: list[AudioRef]
    files: list[FileRef]


class ExtensionPreInstallPayload(TypedDict, total=False):
    """Payload for ``extension.pre_install``."""

    name: str
    version: str
    manifest: dict[str, Any]
    signature: str
    source_url: str


class FederationPreAcceptPayload(TypedDict, total=False):
    """Payload for ``federation.pre_accept``."""

    message: dict[str, Any]
    peer_principal: str
    classification: str
    signature_chain: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Observer event payloads (EventBus subjects)
# ---------------------------------------------------------------------------


class ToolPostInvokePayload(TypedDict, total=False):
    """Payload for ``tool.post_invoke``."""

    tool_name: str
    args: dict[str, Any]
    result: Any
    error: str
    latency_ms: int
    principal: str
    model_id: str
    tokens: int


class PromptPostSubmitPayload(TypedDict, total=False):
    """Payload for ``prompt.post_submit``."""

    messages: list[dict[str, Any]]
    response: dict[str, Any]
    latency_ms: int
    principal: str
    model_id: str
    tokens: int
    cost_usd: float


class CliCommandStartedPayload(TypedDict, total=False):
    """Payload for ``cli.command_started``."""

    command_path: str
    args: list[str]
    principal: str
    started_at: str


class CliCommandEndedPayload(TypedDict, total=False):
    """Payload for ``cli.command_ended``."""

    command_path: str
    exit_code: int
    duration_ms: int
    ended_at: str


class ExtensionPostInstallPayload(TypedDict, total=False):
    """Payload for ``extension.post_install``."""

    name: str
    version: str
    install_path: str
    manifest: dict[str, Any]


class FederationPostAcceptPayload(TypedDict, total=False):
    """Payload for ``federation.post_accept``."""

    message: dict[str, Any]
    peer_principal: str
    accepted_at: str


# ---------------------------------------------------------------------------
# Event-name registries — used by the discovery layer to route hooks to the
# correct primitive (HookBus vs EventBus) without hard-coding.
# ---------------------------------------------------------------------------

#: Subjects routed to `HookBus` (interceptor primitive).
INTERCEPTOR_EVENTS: frozenset[str] = frozenset(
    {
        "tool.pre_invoke",
        "prompt.pre_submit",
        "extension.pre_install",
        "federation.pre_accept",
    },
)

#: Subjects routed to `EventBus` (observer primitive).
OBSERVER_EVENTS: frozenset[str] = frozenset(
    {
        "tool.post_invoke",
        "prompt.post_submit",
        "cli.command_started",
        "cli.command_ended",
        "session.started",
        "session.ended",
        "extension.post_install",
        "federation.post_accept",
    },
)

#: Closed taxonomy — every event the platform itself fires.
PLATFORM_EVENTS: frozenset[str] = INTERCEPTOR_EVENTS | OBSERVER_EVENTS


__all__ = [
    "AudioRef",
    "CliCommandEndedPayload",
    "CliCommandStartedPayload",
    "ExtensionPostInstallPayload",
    "ExtensionPreInstallPayload",
    "FederationPostAcceptPayload",
    "FederationPreAcceptPayload",
    "FileRef",
    "INTERCEPTOR_EVENTS",
    "ImageRef",
    "OBSERVER_EVENTS",
    "PLATFORM_EVENTS",
    "PromptPostSubmitPayload",
    "PromptPreSubmitPayload",
    "ToolPostInvokePayload",
    "ToolPreInvokePayload",
]
