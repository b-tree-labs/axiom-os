# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A video source is a connector of kind ``video`` (ADR-240 D1, ADR-220 §2, spec-video §Connector).

One file per source, in the directory every connector lives in and in the shape
the platform's loader reads, so ``video`` is a connector kind and not a second
registry::

    # <state>/plinth/connectors/cam-1.toml
    [connector]
    name = "cam-1"
    kind = "video"
    credential_ref = "foreign://video-cam-1"   # the login lives in the vault

    [connector.params]
    fps_cap = 10                               # common to every source kind

    [connector.params.source]                  # what the source kind reads
    kind = "rtsp"
    url = "rtsp://cam.example.org:554/stream1"
    transport = "tcp"

``[[connector.params.triggers]]`` tables attach triggers to the source (one per
trigger; ``runner.py`` runs them in the copy that holds the source, and
``connector reload`` applies a change, ``reload.py``).

The lifecycle is ADR-238 D5's: :func:`detect` (delegated to the kind),
:func:`connect` (write the file), :func:`verify` (one frame; the source is not
active until it passes), :func:`health` and the ``status`` view, and
:func:`revoke`.
"""

from __future__ import annotations

import os
import re
import tomllib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .naming import command as _command
from . import state as _state
from .frame import utc_now_iso
from .sources import InvalidSourceConfig, UnknownSourceKind, VideoSourceError, source_kind
from .state import VerifyRecord

if TYPE_CHECKING:
    from axiom.extensions.builtins.connector.detect import DetectResult

    from .sources import VideoSource

#: The connector kind every video source is declared as.
KIND = "video"
DEFAULT_FPS_CAP = 10
MAX_FPS_CAP = 60

#: ADR-220 §3 health, as ``axi connector`` reports it for every connector.
HEALTH = ("ready", "degraded", "failed", "unmounted")

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _cli() -> str:
    from axiom.extensions.builtins.video.naming import cli_name

    return cli_name()


def _state_dir(state_dir: Path | None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir()


class ConnectorNotFound(LookupError):
    """No connector file by that name."""


class NotAVideoConnector(ValueError):
    """A connector by that name exists, of another kind."""


@dataclass(frozen=True)
class VideoConnector:
    """One declared video source."""

    name: str
    source: Mapping[str, Any]
    credential_ref: str | None = None
    fps_cap: int = DEFAULT_FPS_CAP
    triggers: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    @property
    def source_kind(self) -> str:
        return str(self.source.get("kind") or "")

    def source_params(self) -> dict[str, Any]:
        """``[params.source]`` without its ``kind``: what the kind's factory is given."""
        return {k: v for k, v in self.source.items() if k != "kind"}

    def as_toml(self) -> str:
        import tomlkit

        doc = tomlkit.document()
        doc.add(
            tomlkit.comment(
                "A video source (ADR-240). Its login is in the vault, behind credential_ref."
            )
        )
        connector = tomlkit.table()
        connector["name"] = self.name
        connector["kind"] = KIND
        if self.credential_ref:
            connector["credential_ref"] = self.credential_ref
        params = tomlkit.table()
        params["fps_cap"] = self.fps_cap
        source = tomlkit.table()
        source["kind"] = self.source_kind
        for key, value in self.source_params().items():
            source[key] = value
        params["source"] = source
        if self.triggers:
            triggers = tomlkit.aot()
            for trigger in self.triggers:
                table = tomlkit.table()
                for key, value in trigger.items():
                    table[key] = value
                triggers.append(table)
            params["triggers"] = triggers
        connector["params"] = params
        doc["connector"] = connector
        return tomlkit.dumps(doc)

    @classmethod
    def from_toml(cls, text: str) -> VideoConnector:
        connector = tomllib.loads(text).get("connector") or {}
        kind = connector.get("kind")
        if kind != KIND:
            raise NotAVideoConnector(
                f"connector {connector.get('name')!r} is of kind {kind!r}, not {KIND}"
            )
        params = connector.get("params") or {}
        return cls(
            name=str(connector.get("name") or ""),
            source=dict(params.get("source") or {}),
            credential_ref=connector.get("credential_ref") or None,
            fps_cap=params.get("fps_cap", DEFAULT_FPS_CAP),
            triggers=tuple(dict(t) for t in params.get("triggers") or ()),
        )


def connector_path(name: str, *, state_dir: Path | None = None) -> Path:
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import connectors_dir

    return connectors_dir(state_dir=_state_dir(state_dir)) / f"{name}.toml"


def validate(connector: VideoConnector) -> None:
    """Raise if the declaration could never work: name, fps_cap, source kind, the kind's params."""
    name = connector.name
    if not isinstance(name, str) or not _NAME.match(name):
        raise InvalidSourceConfig(
            f"a source name is 1 to 64 lowercase letters, digits, - and _, starting with a letter or digit; "
            f"got {name!r}",
            fix="choose a name like cam-1",
        )
    fps = connector.fps_cap
    if isinstance(fps, bool) or not isinstance(fps, int) or not 1 <= fps <= MAX_FPS_CAP:
        raise InvalidSourceConfig(
            f"fps_cap is a whole number of frames per second from 1 to {MAX_FPS_CAP}, got {fps!r}",
            fix=f"set fps_cap = {DEFAULT_FPS_CAP}",
        )
    if not isinstance(connector.source, Mapping) or not connector.source_kind:
        raise InvalidSourceConfig(
            "the source declares no kind",
            fix='set kind under [connector.params.source], e.g. kind = "rtsp"',
        )
    kind = source_kind(connector.source_kind)  # UnknownSourceKind names the known ones
    kind.validate(connector.source_params())
    if connector.credential_ref:
        from axiom.extensions.builtins.secrets import SecretRef

        try:
            SecretRef.parse(connector.credential_ref)
        except ValueError:
            raise InvalidSourceConfig(
                f"credential_ref {connector.credential_ref!r} is not a secret reference",
                fix="use the reference `vault set` gave, e.g. foreign://video-cam-1",
            ) from None


def save(
    connector: VideoConnector, *, state_dir: Path | None = None, overwrite: bool = False
) -> Path:
    validate(connector)
    path = connector_path(connector.name, state_dir=state_dir)
    if path.exists() and not overwrite:
        raise FileExistsError(f"a connector named {connector.name!r} is already declared at {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(connector.as_toml(), encoding="utf-8")
    os.replace(tmp, path)
    return path


def load(name: str, *, state_dir: Path | None = None) -> VideoConnector:
    path = connector_path(name, state_dir=state_dir)
    if not path.exists():
        raise ConnectorNotFound(
            f"no video source named {name!r} is declared (looked in {path.parent})"
        )
    try:
        return VideoConnector.from_toml(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise InvalidSourceConfig(
            f"{path} is not valid TOML: {exc}", fix=f"fix or remove {path}"
        ) from None


def list_connectors(*, state_dir: Path | None = None) -> list[VideoConnector]:
    """Every declared video source, by name. A file that does not parse is skipped, not fatal."""
    folder = connector_path("x", state_dir=state_dir).parent
    if not folder.exists():
        return []
    out = []
    for path in sorted(folder.glob("*.toml")):
        try:
            out.append(VideoConnector.from_toml(path.read_text(encoding="utf-8")))
        except (OSError, tomllib.TOMLDecodeError, NotAVideoConnector, TypeError, ValueError):
            continue
    return sorted(out, key=lambda c: c.name)


def delete(name: str, *, state_dir: Path | None = None) -> bool:
    try:
        connector_path(name, state_dir=state_dir).unlink()
        return True
    except FileNotFoundError:
        return False


def resolve_credential(ref: str | None) -> Any:
    """The vault secret behind ``ref`` (``None`` for none). Raises ``VideoSourceError`` with the fix."""
    if not ref:
        return None
    from axiom.extensions.builtins.secrets import SecretRef, resolve

    fix = (
        f"store the login with `{_cli()} vault set <credential name>` (it reads username:password from "
        'stdin) and set credential_ref = "foreign://<credential name>", or correct the reference'
    )
    try:
        parsed = SecretRef.parse(ref)
    except ValueError:
        raise VideoSourceError(
            f"credential_ref {ref!r} is not a secret reference",
            code="credential-unresolved",
            fix=fix,
        ) from None
    try:
        return resolve(parsed)
    except Exception as exc:  # noqa: BLE001 - every store fails its own way; the fix is the same
        raise VideoSourceError(
            f"credential_ref {ref!r} did not resolve ({type(exc).__name__})",
            code="credential-unresolved",
            fix=fix,
        ) from None


def build_source(connector: VideoConnector) -> VideoSource:
    """Resolve the credential and build the source its kind describes."""
    kind = source_kind(connector.source_kind)
    return kind.build(connector.source_params(), resolve_credential(connector.credential_ref))


# ---- lifecycle (ADR-238 D5) -------------------------------------------------------


def connect(connector: VideoConnector, *, state_dir: Path | None = None) -> Path:
    """Declare a source: write its file. An existing name is refused, never replaced."""
    return save(connector, state_dir=state_dir, overwrite=False)


def detect(name: str, *, state_dir: Path | None = None) -> DetectResult:
    return build_source(load(name, state_dir=state_dir)).detect()


def verify(name: str, *, state_dir: Path | None = None) -> VerifyRecord:
    """Ask the source for one frame and record the result, pass or fail."""
    sd = _state_dir(state_dir)
    at = utc_now_iso()
    try:
        connector = load(name, state_dir=sd)
    except (ConnectorNotFound, NotAVideoConnector, InvalidSourceConfig) as exc:
        return VerifyRecord(
            ok=False,
            at=at,
            error=str(exc),
            code="not-declared",
            fix=f"declare it with `{_command()} add {name} --kind rtsp --url <rtsp://host/path>`",
        )
    try:
        report = build_source(connector).verify()
        record = VerifyRecord(ok=True, at=at, report=report.as_dict())
    except VideoSourceError as exc:
        record = VerifyRecord(ok=False, at=at, error=str(exc), code=exc.code, fix=exc.fix)
    except UnknownSourceKind as exc:
        record = VerifyRecord(
            ok=False,
            at=at,
            error=str(exc),
            code="invalid-config",
            fix="set a registered kind under [connector.params.source]",
        )
    _state.write_verify_record(name, record, sd)
    return record


def is_active(name: str, *, state_dir: Path | None = None) -> bool:
    record = _state.read_verify_record(name, _state_dir(state_dir))
    return bool(record and record.ok)


def health(name: str, *, state_dir: Path | None = None) -> tuple[str, str]:
    """``(health, reason)`` from the declaration and the last verify alone (status adds the session)."""
    sd = _state_dir(state_dir)
    if not connector_path(name, state_dir=sd).exists():
        return "unmounted", "not declared"
    record = _state.read_verify_record(name, sd)
    if record is None:
        return "unmounted", f"not verified yet: run `{_command()} verify {name}`"
    if not record.ok:
        return "failed", f"{record.error}. Fix: {record.fix}" if record.fix else str(record.error)
    report = record.report or {}
    return (
        "ready",
        f"verified {record.at}: {report.get('codec')} {report.get('width')}x{report.get('height')}",
    )


def revoke(
    name: str, *, state_dir: Path | None = None, wait_s: float | None = None
) -> dict[str, Any]:
    """Stop capture, release the claim, remove the file and its records.

    The event ledger is the record and keeps the source's history; an episode a
    crashed capture left open is closed there first (``runner.REASON_REVOKED``).
    """
    from .control import DRAIN_S, stop
    from .runner import REASON_REVOKED, close_open_episodes

    sd = _state_dir(state_dir)
    declared = connector_path(name, state_dir=sd).exists()
    stopped = stop(name, state_dir=sd, wait_s=DRAIN_S if wait_s is None else wait_s)
    released = _state.release_claim(name, sd)
    removed = delete(name, state_dir=sd)
    _state.clear_verify_record(name, sd)
    _state.clear_live(name, sd)
    _state.clear_log(name, sd)
    close_open_episodes(name, sd, reason=REASON_REVOKED)
    _state.clear_reload_request(name, sd)
    return {
        "name": name,
        "revoked": declared and removed,
        "was_running": stopped["was_running"],
        "claim_released": released or stopped["stopped"],
    }


__all__ = [
    "ConnectorNotFound",
    "DEFAULT_FPS_CAP",
    "HEALTH",
    "KIND",
    "MAX_FPS_CAP",
    "NotAVideoConnector",
    "VideoConnector",
    "build_source",
    "connect",
    "connector_path",
    "delete",
    "detect",
    "health",
    "is_active",
    "list_connectors",
    "load",
    "resolve_credential",
    "revoke",
    "save",
    "validate",
    "verify",
]
