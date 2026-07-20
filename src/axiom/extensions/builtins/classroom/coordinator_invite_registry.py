# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""File-backed coordinator invite registry.

Implements the :class:`InviteRegistry` protocol with a single JSON file
so that ``axi classroom invite`` (short-lived mint process) and
``axi classroom serve`` (long-running HTTP process) can share state
without coordinating via IPC.

Layout of the registry file::

    {
      "invites":  {"<token>": {<invite-token fields>}, ...},
      "consumed": ["<token>", ...]
    }

Writes go through a tempfile + ``os.replace`` so readers never see a
half-written file. Reads re-open the file on every call; that's
negligible for a classroom-sized cohort (<=50 invites) and means a
running coordinator picks up newly minted invites without restart.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .invite_token import InviteToken


@dataclass
class FileInviteRegistry:
    """Durable registry shared between mint + serve CLI commands."""

    path: Path

    # ---- Public API — satisfies InviteRegistry ----

    def register(self, invite: InviteToken) -> None:
        state = self._read()
        state["invites"][invite.token] = asdict(invite)
        self._write(state)

    def find_by_token(self, token: str) -> InviteToken | None:
        state = self._read()
        raw = state["invites"].get(token)
        if raw is None:
            return None
        return _invite_from_dict(raw)

    def is_consumed(self, token: str) -> bool:
        state = self._read()
        return token in state["consumed"]

    def mark_consumed(self, token: str) -> None:
        state = self._read()
        if token not in state["consumed"]:
            state["consumed"].append(token)
            self._write(state)

    # ---- Extras useful to instructor-facing CLI ----

    def list_for_classroom(self, classroom_id: str) -> list[InviteToken]:
        state = self._read()
        return [
            _invite_from_dict(raw)
            for raw in state["invites"].values()
            if raw.get("classroom_id") == classroom_id
        ]

    # ---- Disk I/O ----

    def _read(self) -> dict:
        if not self.path.is_file():
            return {"invites": {}, "consumed": []}
        try:
            raw = json.loads(self.path.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invite registry at {self.path} is corrupt: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ValueError(
                f"invite registry at {self.path} is corrupt: not a JSON object"
            )
        raw.setdefault("invites", {})
        raw.setdefault("consumed", [])
        return raw

    def _write(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            dir=self.path.parent,
            prefix=self.path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as tf:
            json.dump(state, tf, indent=2, sort_keys=True)
            tmp_path = Path(tf.name)
        os.replace(tmp_path, self.path)


def _invite_from_dict(raw: dict) -> InviteToken:
    return InviteToken(
        token=str(raw["token"]),
        classroom_id=str(raw["classroom_id"]),
        coordinator_id=str(raw["coordinator_id"]),
        expires=str(raw["expires"]),
        coordinator_url=(
            str(raw["coordinator_url"]) if raw.get("coordinator_url") else None
        ),
    )


__all__ = ["FileInviteRegistry"]
