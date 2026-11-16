# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Video sources and stream triggers (ADR-240, spec-video).

A video source is a connector of kind ``video`` whose ``source.kind`` names what
it is; ``rtsp``, the live stream of an IP camera, is the first kind. A capture
service holds each started source once, decodes it in an ``ffmpeg`` subprocess and
fans its frames out on an in-process bus. Triggers subscribe to the bus and append
what they see to the node's event ledger; ``motion`` is the first, and a
consumer's trigger is a package that registers the same way. Nothing here plays,
records or stores video, and nothing here interprets it: what a change in view
means is a consumer's trigger.

Public surface::

    from axiom.extensions.builtins.video import (
        Frame, FrameBus, Subscription,                 # what a consumer of frames sees
        VideoSource, VerifyReport, VideoSourceError,   # what a source kind implements
        SourceKind, SourceParam, register_source_kind, source_kind, source_kinds,
        VideoConnector,                                # a declared source
        StreamTrigger, BaseTrigger, TriggerEvent,      # what a trigger implements and emits
        TriggerConfigError, incident_key, SEVERITIES,
    )
"""

from __future__ import annotations

from .bus import FrameBus, Subscription
from .connector import VideoConnector
from .frame import Frame
from .sources import (
    SourceKind,
    SourceParam,
    VerifyReport,
    VideoSource,
    VideoSourceError,
    register_source_kind,
    source_kind,
    source_kinds,
)
from .sources import rtsp as _rtsp
from .triggers import (
    SEVERITIES,
    BaseTrigger,
    StreamTrigger,
    TriggerConfigError,
    TriggerEvent,
    incident_key,
)

# Built-in kinds register at the extension's import (spec-video §VideoSource), the
# same way any other kind does. Registering the same entry again is harmless.
register_source_kind(_rtsp.KIND)

__all__ = [
    "SEVERITIES",
    "BaseTrigger",
    "Frame",
    "FrameBus",
    "SourceKind",
    "SourceParam",
    "StreamTrigger",
    "Subscription",
    "TriggerConfigError",
    "TriggerEvent",
    "VerifyReport",
    "VideoConnector",
    "VideoSource",
    "VideoSourceError",
    "incident_key",
    "register_source_kind",
    "source_kind",
    "source_kinds",
]
