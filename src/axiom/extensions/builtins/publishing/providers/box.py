# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Box.com provider for Publisher — enterprise file collaboration platform.

This provider handles document management on Box.com, supporting:
- File upload/download
- Folder organization
- Comment extraction
- Shared link generation
- Version tracking
- Metadata annotation

Authentication:
- OAuth 2.0 (client credentials or user-initiated flow)
- Requires: BOX_CLIENT_ID, BOX_CLIENT_SECRET, BOX_ENTERPRISE_ID (or user auth)

Configuration:
- Environment variables OR .neut/docflow/config.json:
  {
    "box": {
      "client_id": "...",
      "client_secret": "...",
      "enterprise_id": "..."
    }
  }

Usage:
    from axiom.extensions.builtins.publishing.providers.box import BoxProvider
    provider = BoxProvider()

    # Upload
    result = provider.upload(
        local_path=Path("docs/prds/my-prd.docx"),
        destination="prds/my-prd.docx",
        metadata={"version": "v1", "author": "alice"}
    )

    # Download
    provider.download("file_id_123", Path("local.docx"))

    # Get shareable link
    url = provider.get_canonical_url("file_id_123")

Notes:
- Install: pip install boxsdk
- Box folder IDs are different from SharePoint path-based URLs
- Comments are extracted via API (not embedded in file like Word)
- Metadata stored as custom JSON properties on file
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

try:
    from boxsdk import Client, OAuth2
except ImportError:
    Client = None
    OAuth2 = None


CONFIG_PATH = Path(".neut/publisher/config.json")


def _load_config() -> dict:
    """Load Box credentials from config file or environment."""
    if CONFIG_PATH.exists():
        try:
            config = json.loads(CONFIG_PATH.read_text())
            return config.get("box", {})
        except (json.JSONDecodeError, KeyError):
            pass

    # Fallback to environment variables
    return {
        "client_id": os.getenv("BOX_CLIENT_ID"),
        "client_secret": os.getenv("BOX_CLIENT_SECRET"),
        "enterprise_id": os.getenv("BOX_ENTERPRISE_ID"),
    }


@dataclass
class BoxProvider:
    """Provider for Box.com file storage and collaboration."""

    client_id: str | None = None
    client_secret: str | None = None
    enterprise_id: str | None = None
    access_token: str | None = None

    def __post_init__(self):
        """Initialize Box client."""
        if Client is None:
            raise RuntimeError(
                "boxsdk not installed. Install with: pip install boxsdk"
            )

        cfg = _load_config()
        self.client_id = self.client_id or cfg.get("client_id")
        self.client_secret = self.client_secret or cfg.get("client_secret")
        self.enterprise_id = self.enterprise_id or cfg.get("enterprise_id")

        if not self.client_id or not self.client_secret:
            raise RuntimeError(
                "Missing BOX_CLIENT_ID or BOX_CLIENT_SECRET. "
                "Add to .neut/docflow/config.json or set environment variables."
            )

        # OAuth2 setup (could be user-initiated or service account)
        # For now, this is a placeholder; actual implementation depends on
        # whether using 3-legged (user) or 2-legged (service account) OAuth
        assert OAuth2 is not None  # guarded by Client check above
        self.oauth2 = OAuth2(
            client_id=self.client_id,
            client_secret=self.client_secret,
        )

        # Note: Actual token acquisition would happen in a method
        # (either interactive login or using cached token)
        self.client: Client | None = None  # type: ignore[valid-type]

    def _authenticate(self) -> None:
        """Authenticate with Box API.

        Placeholder: actual implementation would use OAuth2 flow.
        Could be:
        - User-initiated (browser redirect)
        - Service account (JWT)
        - Cached token refresh
        """
        if self.client is None and self.access_token:
            assert Client is not None  # guarded in __post_init__
            self.client = Client(self.oauth2)
        elif self.client is None:
            raise RuntimeError(
                "Box authentication not yet implemented. "
                "Use BoxProvider.authenticate_interactive() or set access_token."
            )

    def authenticate_interactive(self) -> None:
        """Initiate interactive OAuth2 authentication flow.

        Opens browser for user consent, receives authorization code,
        exchanges for access token. Token is cached for future use.
        """
        raise NotImplementedError(
            "Interactive Box authentication coming in future release. "
            "Use service account credentials or set BOX_ENTERPRISE_ID for now."
        )

    def upload(
        self,
        local_path: Path,
        destination: str,
        metadata: dict,
    ) -> dict:
        """Upload file to Box.

        Args:
            local_path: Path to local file
            destination: Box path (e.g., "prds/my-prd.docx")
            metadata: Document metadata (version, author, commit, etc.)
        Returns:
            Dict with file_id, url, version
        """
        raise NotImplementedError(
            "Box upload coming in future release. "
            "Provider skeleton in place for multi-backend architecture."
        )

    def download(self, file_id: str, local_path: Path) -> Path:
        """Download file from Box.

        Args:
            file_id: Box file ID
            local_path: Local path to save
        Returns:
            Path to downloaded file
        """
        raise NotImplementedError(
            "Box download coming in future release."
        )

    def move(self, _file_id: str, _new_destination: str) -> dict:
        """Move/rename file on Box.

        Args:
            file_id: Box file ID
            new_destination: New path
        Returns:
            Updated file metadata
        """
        raise NotImplementedError(
            "Box move coming in future release."
        )

    def get_canonical_url(self, file_id: str) -> str:
        """Get shareable Box link for file.

        Args:
            file_id: Box file ID
        Returns:
            Shareable Box URL
        """
        raise NotImplementedError(
            "Box get_canonical_url coming in future release."
        )

    def list_artifacts(self, prefix: str) -> list[dict]:
        """List files in Box folder.

        Args:
            prefix: Box folder path (e.g., "prds/")
        Returns:
            List of file metadata dicts
        """
        raise NotImplementedError(
            "Box list_artifacts coming in future release."
        )

    def delete(self, file_id: str) -> bool:
        """Delete file from Box.

        Args:
            file_id: Box file ID
        Returns:
            True if deleted
        """
        raise NotImplementedError(
            "Box delete coming in future release."
        )

    def fetch_comments(self, file_id: str) -> list[dict]:
        """Extract comments/feedback from Box file.

        Box API provides comments via /files/{file_id}/comments endpoint.

        Args:
            file_id: Box file ID
        Returns:
            List of comment dicts (author, text, timestamp, resolved)
        """
        raise NotImplementedError(
            "Box fetch_comments coming in future release."
        )

    def set_metadata(self, file_id: str, metadata: dict) -> bool:
        """Store custom JSON metadata on Box file.

        Box allows custom metadata templates; this stores Publisher state
        (version, publication_date, source_md_checksum, etc.) alongside the file.

        Args:
            file_id: Box file ID
            metadata: Custom metadata dict
        Returns:
            True if stored
        """
        raise NotImplementedError(
            "Box set_metadata coming in future release."
        )

    def get_metadata(self, file_id: str) -> dict:
        """Retrieve custom metadata from Box file.

        Args:
            file_id: Box file ID
        Returns:
            Metadata dict
        """
        raise NotImplementedError(
            "Box get_metadata coming in future release."
        )


# ─── Box Provider Notes ───
