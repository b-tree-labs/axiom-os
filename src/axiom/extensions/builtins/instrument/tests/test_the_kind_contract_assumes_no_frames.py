# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""INS-011: the kind contract is the control surface only (ADR-256 D6).

It names no frame, bus, trigger, source kind or connector file, so a kind that
yields readings fits it as well as video does. The stand-in kind here is that
scalar shape.
"""

from __future__ import annotations

import dataclasses
import typing

from axiom.extensions.builtins.instrument import kinds as k

from .fakes import GaugeKind, NotAKind


def _members() -> set[str]:
    return {
        name
        for name in vars(k.InstrumentKind)
        if not name.startswith("_") and name not in ("register",)
    } | set(typing.get_type_hints(k.InstrumentKind))


def test_the_contract_is_exactly_the_control_surface():
    assert _members() == {
        "name",
        "description",
        "yields",
        "skill_for_verb",
        "build_parser",
        "main",
        "list",
        "status",
    }


def test_nothing_in_it_assumes_video():
    words = " ".join(_members()).lower() + " ".join(
        f.name for f in dataclasses.fields(k.InstrumentRow) + dataclasses.fields(k.KindStatus)
    )
    for word in ("frame", "bus", "trigger", "source", "connector", "rtsp", "ffmpeg"):
        assert word not in words, word


def test_a_scalar_shaped_kind_is_a_kind():
    assert isinstance(GaugeKind(), k.InstrumentKind)
    assert not isinstance(NotAKind(), k.InstrumentKind)


def test_rows_and_summaries_are_plain_data():
    row = k.InstrumentRow(kind="gauges", name="temp", health="ready", state="reading")
    assert row.as_dict() == {
        "kind": "gauges",
        "name": "temp",
        "health": "ready",
        "state": "reading",
        "detail": "",
    }
    status = k.KindStatus(kind="gauges", ok=False, summary="no answer", error="e", fix="f")
    assert status.as_dict()["counts"] == {} and status.as_dict()["fix"] == "f"


def test_health_is_the_connector_vocabulary():
    assert k.HEALTH_STATES == ("ready", "degraded", "failed", "unmounted")
