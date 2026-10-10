# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The forwarder: outbox in order → intake, else a drop, else wait. See ``__init__``."""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from ..ingest_sink.edge import EdgeOutbox
from ..ingest_sink.headroom import headroom
from ..sources.edge.puller import FileCursor

#: Marks a request as backlog so the intake charges it to the backlog budget.
#: Kept equal to ``ingest_sink.api.BACKLOG_LANE_HEADER`` (that module needs a
#: web stack; this one must not).
BACKLOG_LANE_HEADER = "X-Axiom-Lane"

log = logging.getLogger("axiom.data.forward")


def request_body(rec: dict, rows: list[dict], *, source: str) -> bytes:
    """The ``POST /ingest/rows`` body for one outbox batch, byte-stable."""
    body = {
        "source": source,
        "batches": [
            {
                "item_id": rec["item_id"],
                "schema_ref": rec["schema_ref"],
                "rows": rows,
                "etag": rec.get("etag"),
                "source_path": rec.get("source_path"),
                "metadata": dict(rec.get("metadata") or {}),
            }
        ],
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def request_body_many(pairs: list[tuple[dict, list[dict]]], *, source: str) -> bytes:
    """One ``POST /ingest/rows`` body carrying several outbox batches of one source."""
    body = {
        "source": source,
        "batches": [
            {
                "item_id": rec["item_id"],
                "schema_ref": rec["schema_ref"],
                "rows": rows,
                "etag": rec.get("etag"),
                "source_path": rec.get("source_path"),
                "metadata": dict(rec.get("metadata") or {}),
            }
            for rec, rows in pairs
        ],
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


# --------------------------------------------------------------------- outbox



def _disk_beat(outbox_disk: dict[str, Any]) -> dict[str, Any]:
    """The disks the upstream judges this site by: the outbox's and every
    watched volume (AXIOM_DISK_WATCH, e.g. the local database's). ``disk_used``
    is the fullest of them, the field the platform's site health reads."""
    from ..ingest_sink.headroom import used_fraction, watched

    disks = ([{"label": "outbox", **outbox_disk}] if outbox_disk else []) + watched()
    used = [u for u in (used_fraction(d) for d in disks) if u is not None]
    return {
        "disk_used": round(max(used), 4) if used else None,
        "disk_alarm": any(d.get("alarm") for d in disks) if disks else None,
        "disks": [
            {"label": d["label"], "used": used_fraction(d), "free_bytes": d.get("free_bytes"),
             "alarm": d.get("alarm"), "error": d.get("error")}
            for d in disks
        ],
    }

class LocalOutbox:
    """This node's own ingest outbox, read from disk."""

    def __init__(
        self, directory: str | os.PathLike, *, bronze_root_for: Callable[[str], Path]
    ) -> None:
        self._outbox = EdgeOutbox(directory)
        self._bronze_root_for = bronze_root_for

    def records(self, after: int, limit: int) -> list[dict]:
        return self._outbox.read(after=after, limit=limit)

    def last_seq(self) -> int:
        return self._outbox.last_seq()

    @property
    def directory(self) -> Path:
        return self._outbox.dir

    def content(self, source: str, content_hash: str) -> bytes:
        blob = EdgeOutbox.content(source, content_hash, bronze_root=self._bronze_root_for(source))
        if hashlib.sha256(blob).hexdigest() != content_hash:
            raise ValueError(f"outbox batch {content_hash[:12]} does not match its recorded hash")
        return blob


# -------------------------------------------------------------------- policy


class SharePolicy(Protocol):
    def allows(self, record: dict, rows: list[dict]) -> list[dict]: ...
    def hold_until(self, record: dict) -> datetime | None: ...


class ShareEverything:
    def allows(self, record: dict, rows: list[dict]) -> list[dict]:
        return rows

    def hold_until(self, record: dict) -> datetime | None:
        return None


# ------------------------------------------------------------------- targets


@dataclass
class Probe:
    ok: bool
    reason: str = ""


def _wait_for(headers: dict) -> float:
    """Seconds the refusal asked us to wait (the shared parser), 5 s when it did not say."""
    from axiom.infra.ratelimit import parse_headers

    window = parse_headers(headers)
    return max(1.0, float(window.retry_after_s)) if window.retry_after_s is not None else 5.0


class IntakeTarget:
    """The upstream node's ``/ingest/rows``, with this site's own key."""

    name = "intake"
    refusal: dict | None = None

    def __init__(
        self, base_url: str, *, token: str, probe_source: str, timeout: float = 30.0
    ) -> None:
        self.base = base_url.rstrip("/")
        self._token = token
        self._probe_source = probe_source
        self.timeout = timeout
        self._client_versions: str | None = None

    def _post(self, body: bytes, *, lane: str = "live") -> tuple[int, dict, dict]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "User-Agent": "axiom-forward",
        }
        if lane == "backlog":
            headers[BACKLOG_LANE_HEADER] = "backlog"
        # What this sender runs, so an intake with a compatibility window can
        # accept it. Without this an intake that declares a minimum refuses
        # every batch, however current the forwarder is.
        from ..compat import HEADER as _COMPAT_HEADER
        from ..compat import client_versions

        if self._client_versions is None:
            self._client_versions = client_versions()
        if self._client_versions:
            headers[_COMPAT_HEADER] = self._client_versions
        req = urllib.request.Request(self.base + "/ingest/rows", data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - configured URL
                raw = resp.read()
                try:
                    return resp.status, dict(json.loads(raw or b"{}")), dict(resp.headers)
                except (ValueError, TypeError):
                    return resp.status, {}, dict(resp.headers)
        except http.client.HTTPException as exc:
            # A response cut short (IncompleteRead, a bad status line) is a
            # transport failure like any other, and callers handle OSError.
            raise ConnectionError(f"{type(exc).__name__}: {exc}") from exc
        except urllib.error.HTTPError as exc:
            # The refusal's own words and its Retry-After are what make a polite
            # retry possible; dropping them is how a client becomes a hot loop.
            try:
                answer = dict(json.loads(exc.read() or b"{}"))
            except (ValueError, TypeError, OSError):
                answer = {}
            return exc.code, answer, dict(exc.headers or {})

    def probe(self) -> Probe:
        try:
            with urllib.request.urlopen(self.base + "/healthz", timeout=10) as resp:  # noqa: S310
                if resp.status != 200:
                    return Probe(False, f"intake health answered {resp.status}")
        except urllib.error.HTTPError as exc:
            return Probe(False, f"intake health answered {exc.code}")
        except OSError as exc:
            return Probe(False, f"intake unreachable: {type(exc).__name__}")
        # An empty batch proves the key is accepted without sending anything.
        try:
            code, _, _ = self._post(json.dumps({"source": self._probe_source, "batches": []}).encode())
        except OSError as exc:
            return Probe(False, f"intake unreachable: {type(exc).__name__}")
        if code in (401, 403):
            return Probe(False, f"the intake refused this site's key (HTTP {code})")
        if code >= 400:
            return Probe(False, f"the intake answered {code} to an empty batch")
        return Probe(True, "")

    def beat(self, beat: dict[str, Any]) -> bool:
        """Post a heartbeat (never data) on the same path and key as the data."""
        from ..ingest_sink.heartbeat import HEARTBEAT_SCHEMA

        body = json.dumps({"source": self._probe_source, "batches": [{
            "item_id": f"hb-{beat.get('node', 'forward')}-{int(time.time())}",
            "schema_ref": HEARTBEAT_SCHEMA, "rows": [beat]}]}, default=str).encode()
        try:
            code, _, _ = self._post(body)
        except OSError:
            return False
        return code == 200

    def send(self, rec: dict, body: bytes, *, lane: str = "live") -> tuple[bool, str]:
        """Deliver one request body. After a polite refusal (426: this node is
        too old for the intake; 429: over this site's budget; 507: the intake
        is short of disk) ``self.refusal`` says which, why, and how long to wait."""
        self.refusal = None
        try:
            code, answer, headers = self._post(body, lane=lane)
        except OSError as exc:
            return False, f"intake unreachable: {type(exc).__name__}"
        if code == 426:
            # Too old for this intake. Not a failure and not a reason to go
            # round it (a fallback would deliver data past the window): hold
            # everything here, say what is required and how, and ask again
            # hourly, because an intake may relax its minimum.
            from ..compat import UPDATE_RECHECK_S

            need = answer.get("detail") if isinstance(answer.get("detail"), dict) else {}
            self.refusal = {"code": 426, "retry_after": UPDATE_RECHECK_S,
                            "reason": str(need.get("detail") or "update required"),
                            "update_required": need}
            return False, f"update required: {need.get('command') or 'update this node'}"
        if code in (429, 503, 507):
            retry = _wait_for(headers)
            # A bare FastAPI app answers {"detail": ...}; a served node wraps
            # it in the shared error envelope.
            detail = str(answer.get("detail") or (answer.get("error") or {}).get("message") or "")
            self.refusal = {"code": code, "retry_after": retry, "reason": detail}
            return False, f"intake answered {code}, retry in {retry:g}s" + (f": {detail}" if detail else "")
        if code != 200:
            return False, f"intake answered {code}"
        # A 200 says the intake handled the request, not that it kept every
        # batch: a batch whose write failed (a full disk) is counted in
        # `errored` and the push still answers 200. Advancing on that would
        # skip data that never landed. Same rule as the DAQ transmitter.
        try:
            errored = int(answer.get("errored", 0) or 0)
            rows_in = int(answer.get("rows_in", 0) or 0)
            kept = int(answer.get("rows_landed", 0) or 0) + int(
                answer.get("rows_duplicate", 0) or 0
            )
        except (TypeError, ValueError):
            return False, "intake answered 200 with an unreadable result"
        if errored > 0:
            return (
                False,
                f"intake could not store {errored} batch(es) (errored={errored}); will retry",
            )
        if kept < rows_in:
            return False, f"intake kept {kept} of {rows_in} rows; will retry"
        return True, ""


class BoxDropTarget:
    """A Box folder, through rclone with the operator's own Box login.

    Box refresh tokens are single use: every refresh returns a new one. So the
    login cannot be handed to rclone read-only (an environment variable) or it
    dies at the first refresh. Each pass writes a private, temporary rclone
    config seeded from ``load_token()``, and after rclone runs, a rotated token
    is handed to ``save_token()`` (the vault), then the file is removed. The
    vault stays the only lasting copy.

    With no token functions (``remote_path`` is a local folder, as in tests),
    rclone is called directly.
    """

    name = "box"

    def __init__(
        self,
        remote_path: str,
        *,
        load_token: Callable[[], str] | None = None,
        save_token: Callable[[str], None] | None = None,
        rclone: str = "rclone",
        env: dict[str, str] | None = None,
    ) -> None:
        self.remote_path = remote_path.rstrip("/")
        self._load, self._save = load_token, save_token
        self._rclone = rclone
        self._env = {**os.environ, **(env or {})}

    def _run(self, *args: str, data: bytes | None = None) -> subprocess.CompletedProcess:
        if self._load is None:
            return subprocess.run(
                [self._rclone, *args], input=data, capture_output=True, env=self._env, timeout=300
            )
        import configparser
        import tempfile

        token = self._load()
        with tempfile.TemporaryDirectory(prefix="axiom-forward-") as d:
            conf = Path(d) / "rclone.conf"
            fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(f"[fwdbox]\ntype = box\ntoken = {token}\n")
            try:
                r = subprocess.run(
                    [self._rclone, "--config", str(conf), *args],
                    input=data,
                    capture_output=True,
                    env=self._env,
                    timeout=300,
                )
            finally:
                cp = configparser.ConfigParser()
                cp.read(conf)
                new = cp.get("fwdbox", "token", fallback=token)
                if new and new != token and self._save is not None:
                    self._save(new)  # the rotated login, back to the vault before the file goes
            return r

    def _dest(self, name: str) -> str:
        base = f"fwdbox:{self.remote_path}" if self._load is not None else self.remote_path
        return f"{base}/{name}" if name else base

    @staticmethod
    def _explain(stderr: bytes, doing: str) -> str:
        """What went wrong, in the terms of what the person should do.

        An expired or revoked login needs the person to reconnect Box; a
        network outage needs nothing but time. rclone's own text names its
        own repair command, which is not the step a partner should run.
        """
        text = stderr.decode(errors="replace")
        low = text.lower()
        if any(
            k in low
            for k in (
                "invalid_grant",
                "couldn't fetch token",
                "cannot fetch token",
                "token expired",
                "unauthorized",
                "401",
                "invalid_token",
            )
        ):
            return (
                f"Box login expired or revoked ({doing}): reconnect Box with the one-time Box "
                "connect step; batches stay queued and go once it is reconnected"
            )
        if any(
            k in low
            for k in (
                "connection refused",
                "no such host",
                "timeout",
                "proxyconnect",
                "network is unreachable",
                "dial tcp",
                "i/o timeout",
                "tls handshake",
            )
        ):
            return f"Box unreachable ({doing}): batches stay queued and go when the network returns"
        tail = text.strip().splitlines()[-1][-160:] if text.strip() else "no detail"
        return f"box drop unavailable ({doing}): {tail}"

    def probe(self) -> Probe:
        try:
            r = self._run("lsjson", "--max-depth", "1", self._dest(""))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Probe(
                False,
                f"Box unreachable (checking the folder): {type(exc).__name__}; batches stay queued",
            )
        if r.returncode != 0:
            if b"directory not found" in r.stderr.lower():
                return Probe(
                    False,
                    f"Box folder not found: {self.remote_path}; create it or correct the path "
                    "(batches stay queued)",
                )
            return Probe(False, self._explain(r.stderr, "checking the folder"))
        return Probe(True, "")

    def send(self, rec: dict, body: bytes, *, first_seq: int | None = None) -> tuple[bool, str]:
        """Drop one body. ``rec`` is its last outbox record; ``first_seq`` its first when it packs several."""
        last = int(rec["seq"])
        if first_seq is None or int(first_seq) == last:
            name = f"{int(rec['seq']):010d}-{rec['content_hash']}.rows.json"
        else:
            name = f"{int(first_seq):010d}-{last:010d}-{hashlib.sha256(body).hexdigest()}.rows.json"
        dest = self._dest(f"{rec['source']}/{name}")
        try:
            r = self._run("rcat", dest, data=body)
            if r.returncode != 0:
                return False, self._explain(r.stderr, "uploading")
            check = self._run("cat", dest)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"box upload failed: {type(exc).__name__}"
        if (
            check.returncode != 0
            or hashlib.sha256(check.stdout).digest() != hashlib.sha256(body).digest()
        ):
            return False, "box upload could not be confirmed"
        return True, ""


# ----------------------------------------------------------------- forwarder


@dataclass
class ForwardReport:
    sent_intake: int = 0
    sent_box: int = 0
    excluded_by_policy: int = 0
    held_by_policy: int = 0
    after: int = 0
    current: str = "waiting"
    reason: str = ""
    #: The pass stopped at its time slice with more to send: run the next one now.
    more: bool = False


@dataclass
class _State:
    current: str = "waiting"
    since: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    reason: str = "starting"
    healthy_in_a_row: int = 0
    failed_in_a_row: int = 0
    switches: list[dict] = field(default_factory=list)


class Forwarder:
    """Deliver an outbox upstream, one durable batch at a time.

    **After an outage, live first.** When the oldest undelivered batch is older
    than ``live_window_s`` the forwarder is catching up. It notes the outbox
    head at that moment (the *split*) and from then on keeps two positions:

    * the **live lane** sends everything after the split, oldest first, one
      batch per request, at the start of every pass, so data collected now is
      never queued behind data collected last week;
    * the **backlog lane** sends from the cursor up to the split, oldest first,
      several batches per request, marked as backlog so the intake charges them
      to a separate per-site budget, for at most ``backlog_slice_s`` a pass.

    When the backlog reaches the split the two join and the cursor jumps to the
    live position. Both positions only ever advance over batches the upstream
    confirmed, and both are on disk (``catchup.json`` beside the cursor), so a
    restart mid-drain resumes without loss; anything resent is deduplicated
    upstream by content hash.

    **Polite.** A 429 pauses the backlog lane for exactly the ``Retry-After``
    it carried, and live keeps going. A 507 (the intake is short of disk) or a
    503 pauses everything for its ``Retry-After``. A refusal is a healthy
    intake answering, so it never counts toward leaving the intake.
    """

    def __init__(
        self,
        outbox: LocalOutbox,
        *,
        cursor: FileCursor,
        intake: IntakeTarget | None,
        box: BoxDropTarget | None = None,
        policy: SharePolicy | None = None,
        source_map: dict[str, str] | None = None,
        status_path: str | os.PathLike | None = None,
        up_after: int = 3,
        down_after: int = 2,
        page: int = 200,
        live_window_s: float = 60.0,
        backlog_records_per_request: int = 50,
        backlog_max_rows: int = 20_000,
        backlog_slice_s: float = 0.5,
        backlog_request_target_s: float = 0.25,
        probe_every_s: float = 10.0,
        lane_slice_s: float = 5.0,
        box_records_per_drop: int = 50,
        box_max_bytes: int = 8 * 2**20,
        beat_node: str | None = None,
        beat_every_s: float = 60.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.outbox, self.cursor = outbox, cursor
        self.intake, self.box = intake, box
        self.policy = policy or ShareEverything()
        self.source_map = dict(source_map or {})
        self.status_path = Path(status_path) if status_path else None
        self.up_after, self.down_after, self.page = up_after, down_after, page
        self.live_window_s = live_window_s
        self.backlog_records_per_request = max(1, min(int(backlog_records_per_request), 500))
        self.backlog_max_rows = backlog_max_rows
        self.backlog_slice_s = backlog_slice_s
        self.backlog_request_target_s = backlog_request_target_s
        self._per_request = self.backlog_records_per_request
        self.probe_every_s = probe_every_s
        # A pass sends for at most this long, then returns to choose(): a slow
        # drop folder must never keep the forwarder from re-asking the intake.
        self.lane_slice_s = lane_slice_s
        self.box_records_per_drop = max(1, int(box_records_per_drop))
        self.box_max_bytes = max(1, int(box_max_bytes))
        self.beat_node, self.beat_every_s = beat_node, beat_every_s
        self._last_beat = 0.0
        self._now = now
        self._clock = clock
        self.state = _State()
        self._last_ok = 0.0
        self._rate_window: list[tuple[float, int]] = []
        self._catchup_path = Path(cursor.path).with_name("catchup.json")
        self.catch_up: dict[str, Any] = self._load_catch_up()
        self.refusal: dict | None = None
        self._load_state()

    # -- status ---------------------------------------------------------------

    def _load_state(self) -> None:
        if self.status_path and self.status_path.exists():
            try:
                d = json.loads(self.status_path.read_text())
                self.state.current = d.get("current", "waiting")
                self.state.since = d.get("since", self.state.since)
                self.state.switches = list(d.get("switches", []))[-20:]
                self.refusal = d.get("refusal")
            except (OSError, ValueError):
                pass

    def _load_catch_up(self) -> dict[str, Any]:
        try:
            return dict(json.loads(self._catchup_path.read_text()))
        except (OSError, ValueError):
            return {"active": None}

    def _save_catch_up(self) -> None:
        self._catchup_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._catchup_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.catch_up))
        tmp.replace(self._catchup_path)

    def health(self) -> dict[str, Any]:
        try:
            disk = headroom(self.outbox.directory)
        except (OSError, AttributeError):
            disk = None
        return {
            "current": self.state.current,
            "since": self.state.since,
            "reason": self.state.reason,
            "after": self.cursor.get(),
            "switches": self.state.switches[-20:],
            "intake_configured": self.intake is not None,
            "box_configured": self.box is not None,
            "catch_up": dict(self.catch_up),
            "refusal": self.refusal,
            "local_disk": disk,
        }

    def _write_status(self, report: ForwardReport) -> None:
        if not self.status_path:
            return
        d = {**self.health(), "last_pass": self._now().isoformat(), "last_report": report.__dict__}
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2, default=str))
        tmp.replace(self.status_path)

    def _switch(self, to: str, reason: str) -> None:
        if to == self.state.current:
            self.state.reason = reason
            return
        entry = {
            "at": self._now().isoformat(),
            "from": self.state.current,
            "to": to,
            "reason": reason,
        }
        log.info("forward: %s -> %s (%s)", self.state.current, to, reason)
        self.state.switches.append(entry)
        self.state.current, self.state.since, self.state.reason = to, entry["at"], reason

    # -- choosing a transport ------------------------------------------------

    def _fallback(self, reason: str) -> None:
        if self.box is not None:
            b = self.box.probe()
            if b.ok:
                self._switch("box", reason)
                return
            reason = f"{reason}; {b.reason}"
        self._switch("waiting", reason)

    def choose(self) -> str:
        if self.intake is None:
            self._fallback("no intake configured")
            return self.state.current
        # An intake that confirmed a delivery moments ago is healthy: probing it
        # on every pass would spend the site's request budget on questions.
        if self.state.current == "intake" and self._clock() - self._last_ok < self.probe_every_s:
            return self.state.current
        p = self.intake.probe()
        if p.ok:
            self.state.healthy_in_a_row += 1
            self.state.failed_in_a_row = 0
            if self.state.current == "intake" or self.state.healthy_in_a_row >= self.up_after:
                self._switch("intake", "intake healthy")
            elif self.state.current != "intake":
                self._fallback(
                    f"intake healthy {self.state.healthy_in_a_row}/{self.up_after} checks"
                )
        else:
            self.state.failed_in_a_row += 1
            self.state.healthy_in_a_row = 0
            if self.state.current != "intake" or self.state.failed_in_a_row >= self.down_after:
                self._fallback(p.reason)
            else:
                self.state.reason = f"{p.reason} ({self.state.failed_in_a_row}/{self.down_after})"
        return self.state.current

    # -- delivering ------------------------------------------------------------

    def _prepare(self, rec: dict, report: ForwardReport) -> str | list[dict]:
        """``"hold"``, ``"exclude"``, or the rows the policy shares."""
        hold = self.policy.hold_until(rec)
        if hold is not None and hold > self._now():
            report.held_by_policy += 1
            return "hold"
        rows = self.policy.allows(rec, json.loads(self.outbox.content(rec["source"], rec["content_hash"])))
        if not rows:
            report.excluded_by_policy += 1
            return "exclude"
        return rows

    def _failed(self, current: str, why: str, report: ForwardReport) -> None:
        report.reason = why
        target = self.intake if current == "intake" else None
        refusal = getattr(target, "refusal", None)
        if refusal:
            # A polite refusal: the intake is healthy and asked us to wait.
            now = self._clock()
            refusal = {**refusal, "at": now}
            self.refusal = refusal
            key = f"refused_{refusal['code']}"
            if self.catch_up.get("active"):
                self.catch_up[key] = int(self.catch_up.get(key, 0)) + 1
            until = now + float(refusal["retry_after"])
            if refusal["code"] == 429 and self.catch_up.get("active"):
                self.catch_up["backlog_wait_until"] = until
            else:
                self.catch_up["wait_until"] = until
            self.state.reason = why
            return
        if current == "intake":
            self.state.failed_in_a_row += 1
            if self.state.failed_in_a_row >= self.down_after:
                self._fallback(why)
        else:
            self.state.reason = why

    def _sent(self, current: str, report: ForwardReport, n: int = 1) -> None:
        if current == "intake":
            report.sent_intake += n
            self._last_ok = self._clock()
            self.state.failed_in_a_row = 0
        else:
            report.sent_box += n

    def _sequential(self, current: str, target: Any, after: int, report: ForwardReport,
                    *, upto: int | None = None, on_advance: Callable[[int], None],
                    deadline: float | None = None) -> tuple[int, str]:
        """Send from ``after`` in order, for at most one time slice.

        Returns ``(position, outcome)``: ``"done"`` (nothing left), ``"stopped"``
        (a failure or a policy hold) or ``"paused"`` (the slice ran out with more
        to send). The status file is written after every confirmed request, so
        it shows a drain while it happens, not only when a pass began.
        """
        if deadline is None:
            deadline = self._clock() + self.lane_slice_s
        while True:
            limit = self.page if upto is None else min(self.page, upto - after)
            if limit <= 0:
                return after, "done"
            records = self.outbox.records(after=after, limit=limit)
            if not records:
                return after, "done"
            if current == "box":
                after, outcome = self._box_page(target, records, after, report, on_advance, deadline)
            else:
                after, outcome = self._intake_page(target, records, after, report, on_advance, deadline)
            if outcome != "more":
                return after, outcome

    def _progress(self, current: str, report: ForwardReport) -> None:
        report.after = self.cursor.get()
        self._write_status(report)
        if current == "intake":
            self._maybe_beat()

    def _intake_page(self, target: Any, records: list[dict], after: int, report: ForwardReport,
                     on_advance: Callable[[int], None], deadline: float) -> tuple[int, str]:
        """One batch per request; ``"more"`` when the page is through within the slice."""
        for rec in records:
            rows = self._prepare(rec, report)
            if rows == "hold":
                return after, "stopped"
            if rows != "exclude":
                body = request_body(rec, rows, source=self.source_map.get(rec["source"], rec["source"]))
                ok, why = target.send(rec, body)
                if not ok:
                    self._failed("intake", why, report)
                    return after, "stopped"
                self._sent("intake", report)
            after = int(rec["seq"])
            on_advance(after)  # only after the upstream confirmed it
            if self._clock() >= deadline:
                self._progress("intake", report)
                return after, "paused"
        self._progress("intake", report)
        return after, "more"

    def _box_page(self, target: Any, records: list[dict], after: int, report: ForwardReport,
                  on_advance: Callable[[int], None], deadline: float) -> tuple[int, str]:
        """Many outbox batches per drop file, in order.

        A drop is the same body the intake takes (``request_body_many``), so
        the upstream lands it exactly as a POST, deduplicating by content
        hash. One rclone round trip per record could not keep up with a live
        collector; a file per ``box_records_per_drop`` records can.
        """
        pack: list[tuple[dict, list[dict]]] = []
        size, last, first_seq = 0, after, None

        def flush() -> bool:
            nonlocal pack, size, first_seq, after
            if pack:
                src = pack[0][0]["source"]
                body = request_body_many(pack, source=self.source_map.get(src, src))
                ok, why = target.send(pack[-1][0], body, first_seq=first_seq)
                if not ok:
                    self._failed("box", why, report)
                    return False
                self._sent("box", report, len(pack))
            if last > after:
                after = last
                on_advance(after)  # only after the drop was read back and matched
                self._progress("box", report)
            pack, size, first_seq = [], 0, None
            return True

        for rec in records:
            rows = self._prepare(rec, report)
            if rows == "hold":
                flush()  # what came before the held batch still goes
                return after, "stopped"
            if rows != "exclude":
                n = len(json.dumps(rows, separators=(",", ":")))
                if pack and (
                    rec["source"] != pack[0][0]["source"]
                    or len(pack) >= self.box_records_per_drop
                    or size + n > self.box_max_bytes
                ):
                    if not flush():
                        return after, "stopped"
                    if self._clock() >= deadline:
                        return after, "paused"
                if first_seq is None:
                    first_seq = int(rec["seq"])
                pack.append((rec, rows))
                size += n
            last = int(rec["seq"])
        if not flush():
            return after, "stopped"
        if self._clock() >= deadline:
            return after, "paused"
        return after, "more"

    def _backlog_step(self, report: ForwardReport) -> bool:
        """One backlog request, oldest first. True if the lane may go on this pass.

        The request size adapts so one request takes about
        ``backlog_request_target_s``: live data waits behind at most one backlog
        request, so that is what bounds live latency during a drain, whatever
        the intake's write speed."""
        cu = self.catch_up
        split = int(cu["split"])
        low = self.cursor.get()
        if low >= split:
            return False
        want = max(1, min(self._per_request, split - low))
        records = self.outbox.records(after=low, limit=want)
        if not records:
            return False
        pack: list[tuple[dict, list[dict]]] = []
        last, rows_in_pack, held = low, 0, False
        for rec in records:
            if pack and rec["source"] != pack[0][0]["source"]:
                break
            rows = self._prepare(rec, report)
            if rows == "hold":
                held = True
                break
            if rows != "exclude":
                if pack and rows_in_pack + len(rows) > self.backlog_max_rows:
                    break
                pack.append((rec, rows))
                rows_in_pack += len(rows)
            last = int(rec["seq"])
        if pack:
            src = pack[0][0]["source"]
            body = request_body_many(pack, source=self.source_map.get(src, src))
            t = self._clock()
            ok, why = self.intake.send(pack[-1][0], body, lane="backlog")  # type: ignore[union-attr]
            took = self._clock() - t
            if not ok:
                self._failed("intake", why, report)
                return False
            self._sent("intake", report, len(pack))
            if took > self.backlog_request_target_s and self._per_request > 1:
                self._per_request = max(1, self._per_request // 2)
            elif took < self.backlog_request_target_s / 2:
                self._per_request = min(self.backlog_records_per_request, self._per_request * 2)
        if last > low:
            cu["drained"] = int(cu.get("drained", 0)) + (last - low)
            self.cursor.set(last)  # only after the upstream confirmed it
            self._save_catch_up()
        self._update_eta(self.cursor.get())
        return not held

    def _update_eta(self, low: int) -> None:
        cu = self.catch_up
        now = self._clock()
        drained = int(cu.get("drained", 0))
        self._rate_window.append((now, drained))
        self._rate_window = [(t, d) for t, d in self._rate_window if now - t <= 30.0]
        t0, d0 = self._rate_window[0]
        if now - t0 >= 2.0 and drained > d0:
            rate = (drained - d0) / (now - t0)
            cu["records_per_s"] = round(rate, 3)
            cu["remaining"] = max(0, int(cu["split"]) - low)
            cu["eta_s"] = round(cu["remaining"] / rate, 1)
        else:
            cu["remaining"] = max(0, int(cu["split"]) - low)

    def _maybe_start_catch_up(self) -> None:
        if self.catch_up.get("active"):
            return
        low = self.cursor.get()
        first = self.outbox.records(after=low, limit=1)
        if not first:
            return
        try:
            age = self._clock() - datetime.fromisoformat(first[0]["received_at"]).timestamp()
        except (KeyError, ValueError, TypeError):
            return
        if age <= self.live_window_s:
            return
        head = self._last_old_seq(low, self.outbox.last_seq())
        now = self._clock()
        previous = {k: v for k, v in self.catch_up.items() if k != "previous"} if "split" in self.catch_up else None
        self.catch_up = {
            "previous": previous,
            "active": True, "split": head, "live_after": head, "started_at": now,
            "backlog_total": head - low, "drained": 0, "remaining": head - low,
            "oldest_pending_at": first[0]["received_at"], "refused_429": 0, "refused_507": 0,
            "records_per_s": None, "eta_s": None,
        }
        self._rate_window = [(now, 0)]
        log.info("forward: catching up %d batches since %s; live first", head - low, first[0]["received_at"])
        self._save_catch_up()

    def _received(self, seq: int) -> float | None:
        rec = self.outbox.records(after=seq - 1, limit=1)
        try:
            return datetime.fromisoformat(rec[0]["received_at"]).timestamp() if rec else None
        except (KeyError, ValueError, TypeError):
            return None

    def _last_old_seq(self, low: int, head: int) -> int:
        """The last batch older than the live window: the split.

        Batches after it arrived within the window, so they are live and go
        first. Without this, a reading that arrived a second before the
        reconnect would wait behind the whole outage. A binary search over
        arrival time; the outbox is in arrival order."""
        cutoff = self._clock() - self.live_window_s
        lo, hi = low + 1, head  # lo is known old (the caller checked it)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            t = self._received(mid)
            if t is not None and t <= cutoff:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def _finish_catch_up(self, position: int) -> None:
        cu = self.catch_up
        self.cursor.set(max(position, self.cursor.get()))
        cu.update(active=False, finished_at=self._clock(), remaining=0, eta_s=0)
        log.info("forward: caught up (%s batches)", cu.get("backlog_total"))
        self._save_catch_up()

    def forward_once(self) -> ForwardReport:
        current = self.choose()
        report = ForwardReport(after=self.cursor.get(), current=current, reason=self.state.reason)
        target = {"intake": self.intake, "box": self.box}.get(current)
        if target is None:
            self._write_status(report)
            return report
        if current == "intake":
            self._forward_intake(report)
            self._maybe_beat()
        else:
            def advance(seq: int) -> None:
                self.cursor.set(seq)

            after, outcome = self._sequential(current, target, self.cursor.get(), report, on_advance=advance)
            report.more = outcome == "paused"
            # A drop carries everything in order, so a catch-up ends when it is through.
            if outcome == "done" and self.catch_up.get("active"):
                self._finish_catch_up(after)
        report.after = self.cursor.get()
        report.current = self.state.current
        self._write_status(report)
        return report

    def beat_fields(self) -> dict[str, Any]:
        """What the upstream sees of this forwarder: catch-up progress and the
        local disk, so a site catching up (or filling) is visible from the
        receiving side, not only on the site's own console."""
        h = self.health()
        cu, disk, refusal = h["catch_up"], h["local_disk"] or {}, h["refusal"] or {}
        return {
            "node": self.beat_node, "role": "forward", "transport": h["current"],
            "delivered_through": h["after"],
            "catch_up_active": bool(cu.get("active")),
            "catch_up_remaining": cu.get("remaining"), "catch_up_total": cu.get("backlog_total"),
            "catch_up_records_per_s": cu.get("records_per_s"), "catch_up_eta_s": cu.get("eta_s"),
            "catch_up_oldest_pending_at": cu.get("oldest_pending_at") if cu.get("active") else None,
            "disk_free_bytes": disk.get("free_bytes"), "disk_alarm": disk.get("alarm"),
            "last_refusal": refusal.get("code"),
            **_disk_beat(disk),
        }

    def _maybe_beat(self) -> None:
        if not self.beat_node or self.intake is None:
            return
        if self._clock() - self._last_beat < self.beat_every_s:
            return
        self._last_beat = self._clock()
        if not self.intake.beat(self.beat_fields()):
            log.info("forward: heartbeat not accepted; data delivery is unaffected")

    def _forward_intake(self, report: ForwardReport) -> None:
        if self._clock() < float(self.catch_up.get("wait_until") or 0):
            report.reason = self.state.reason = (
                f"the intake asked us to wait until {datetime.fromtimestamp(self.catch_up['wait_until'], UTC).isoformat()}"
            )
            return
        self._maybe_start_catch_up()
        cu = self.catch_up
        if not cu.get("active"):
            _, outcome = self._sequential("intake", self.intake, self.cursor.get(), report,
                                          on_advance=lambda seq: self.cursor.set(seq))
            report.more = outcome == "paused"
            return

        def advance_live(seq: int) -> None:
            cu["live_after"] = seq
            self._save_catch_up()

        # Live first, and again between backlog requests, so a reading taken
        # now waits behind at most one backlog request.
        deadline = self._clock() + self.backlog_slice_s
        live_deadline = self._clock() + self.lane_slice_s
        while True:
            _, outcome = self._sequential("intake", self.intake, int(cu["live_after"]), report,
                                          on_advance=advance_live, deadline=live_deadline)
            if outcome == "paused":
                report.more = True
                break
            now = self._clock()
            if now < float(cu.get("wait_until") or 0) or now < float(cu.get("backlog_wait_until") or 0):
                break
            if now >= deadline or not self._backlog_step(report):
                break
        if self.cursor.get() >= int(cu["split"]):
            self._finish_catch_up(int(cu["live_after"]))


__all__ = [
    "BoxDropTarget",
    "ForwardReport",
    "Forwarder",
    "IntakeTarget",
    "LocalOutbox",
    "Probe",
    "SharePolicy",
    "ShareEverything",
    "request_body",
    "request_body_many",
]
