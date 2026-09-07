# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Community knowledge pack — downloadable baseline knowledge for standalone installs.

On first ``axi setup``, offers to download a community pack containing
public-tier agent patterns and reference data.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Community pack location — GitHub Release asset
# Updated with each release
COMMUNITY_PACK_URL = (
    "https://github.com/b-tree-labs/axiom-os/releases/latest/download/community-knowledge.axiompack"
)
# Fallback consumer-repo release. Overridable via AXIOM_COMMUNITY_PACK_REPO so a
# domain consumer can point at its own release without code changes.
_DEFAULT_COMMUNITY_PACK_REPO = "https://github.com/example-org/example-consumer"
COMMUNITY_PACK_FALLBACK = (
    os.environ.get("AXIOM_COMMUNITY_PACK_REPO", _DEFAULT_COMMUNITY_PACK_REPO).rstrip("/")
    + "/releases/latest/download/community-knowledge.axiompack"
)


@dataclass
class CommunityPackStatus:
    available: bool
    installed: bool
    version: str = ""
    fact_count: int = 0
    pattern_count: int = 0
    url: str = ""

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "installed": self.installed,
            "version": self.version,
            "fact_count": self.fact_count,
            "pattern_count": self.pattern_count,
        }


def check_community_pack() -> CommunityPackStatus:
    """Check if community knowledge pack is installed."""
    marker = Path.home() / ".axi" / "community-pack-installed"

    installed = marker.exists()
    version = ""
    if installed:
        try:
            version = marker.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    return CommunityPackStatus(
        available=True,
        installed=installed,
        version=version,
        url=COMMUNITY_PACK_URL,
    )


def download_community_pack(progress_callback=None) -> Path:
    """Download the community knowledge pack."""
    import requests

    dest = Path.home() / ".axi" / "downloads"
    dest.mkdir(parents=True, exist_ok=True)
    pack_path = dest / "community-knowledge.axiompack"

    if progress_callback:
        progress_callback("Downloading community knowledge pack...")

    for url in [COMMUNITY_PACK_URL, COMMUNITY_PACK_FALLBACK]:
        try:
            resp = requests.get(url, stream=True, timeout=30, allow_redirects=True)
            if resp.status_code == 200:
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                with open(pack_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total:
                            pct = int(downloaded / total * 100)
                            progress_callback(f"Downloading... {pct}%")
                return pack_path
        except Exception:
            continue

    raise ConnectionError("Could not download community pack from any source")


def install_community_pack(pack_path: Path, progress_callback=None) -> dict:
    """Install a downloaded community knowledge pack.

    Installs agent patterns from the pack into ``~/.axi/agents/*/patterns.json``.
    """
    result: dict = {"patterns": {}, "facts": 0}

    if progress_callback:
        progress_callback("Installing agent patterns...")

    try:
        from axiom.agents.sharing import AgentPatternResource

        resource = AgentPatternResource()
        pattern_result = resource.import_patterns(pack_path)
        result["patterns"] = pattern_result
    except Exception as e:
        result["pattern_error"] = str(e)

    # Mark as installed
    marker = Path.home() / ".axi" / "community-pack-installed"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(datetime.now(UTC).isoformat())

    if progress_callback:
        progress_callback("Community pack installed.")

    return result


def offer_community_pack(callback=None) -> bool:
    """Offer to download community pack during setup.

    Returns True if installed, False if skipped.
    """
    status = check_community_pack()

    if status.installed:
        if callback:
            callback(f"Community knowledge pack already installed ({status.version})")
        return True

    # Auto-download and install — no prompt needed.
    # Community pack provides essential baseline knowledge.
    if callback:
        callback("Installing community knowledge pack...")

    try:
        pack_path = download_community_pack(callback)
        result = install_community_pack(pack_path, callback)
        if callback:
            callback(f"  Installed: {result['patterns'].get('new', 0)} new patterns")
        return True
    except Exception as e:
        if callback:
            callback(f"  Could not download community pack: {e}")
            callback("  You can install it later with: axi pack install <path>")
        return False
