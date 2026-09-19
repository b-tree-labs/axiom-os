# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Pack server client — interact with remote pack registries."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import requests

from axiom.vega.federation.packs import PACK_EXTENSION


@dataclass
class PackServer:
    """Client for a remote axiompack registry."""

    url: str
    name: str = ""
    access_tier: str = "public"
    api_key: str = ""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @property
    def _base(self) -> str:
        return self.url.rstrip("/")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_packs(self) -> list[dict]:
        """GET /packs/registry.json — list available packs."""
        resp = requests.get(
            f"{self._base}/packs/registry.json",
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def download_pack(
        self,
        pack_id: str,
        version: str = "latest",
        dest: Path | None = None,
    ) -> Path:
        """GET /packs/{pack_id}/{version}.axiompack — download a pack."""
        url = f"{self._base}/packs/{pack_id}/{version}{PACK_EXTENSION}"
        resp = requests.get(
            url,
            headers=self._headers(),
            timeout=120,
            stream=True,
        )
        resp.raise_for_status()

        if dest is None:
            dest = Path.cwd() / f"{pack_id}-{version}{PACK_EXTENSION}"
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)

        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        return dest

    def publish_pack(self, archive_path: Path) -> dict:
        """POST /packs/ — upload a pack to the registry."""
        archive_path = Path(archive_path)
        with open(archive_path, "rb") as f:
            resp = requests.post(
                f"{self._base}/packs/",
                headers=self._headers(),
                files={"pack": (archive_path.name, f, "application/gzip")},
                timeout=120,
            )
        resp.raise_for_status()
        return resp.json()

    def health_check(self) -> bool:
        """Check if the pack server is reachable."""
        try:
            resp = requests.get(
                f"{self._base}/health",
                headers=self._headers(),
                timeout=10,
            )
            return resp.ok
        except requests.RequestException:
            return False
