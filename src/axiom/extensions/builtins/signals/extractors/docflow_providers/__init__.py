# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""DocFlow Provider Registry — Extensible document sync providers.

Design Principles:
1. **Convention over Configuration**: Standard folder structure, naming, env vars
2. **Plugin Architecture**: New providers register via entry points or explicit registration
3. **Two Provider Types**: DocProvider (single docs) and FolderSyncProvider (entire folders)
4. **Credential Management**: Environment variables with standard naming

Provider Types:
- DocProvider: Fetch/sync individual documents (Google Docs, Box Docs, MS Word)
- FolderSyncProvider: Sync entire folders (Dropbox, Google Drive, OneDrive, Box)

Folder Structure Convention:
    inbox/raw/docflow/
        {provider_slug}/           # e.g., google_drive/, dropbox/, box/
            {synced_files}

Environment Variable Convention:
    DOCFLOW_{PROVIDER}_TOKEN       # OAuth access token
    DOCFLOW_{PROVIDER}_CLIENT_ID   # OAuth client ID (if needed)
    DOCFLOW_{PROVIDER}_SECRET      # OAuth client secret (if needed)
    DOCFLOW_{PROVIDER}_REFRESH     # OAuth refresh token (if needed)
    DOCFLOW_{PROVIDER}_FOLDER_ID   # Root folder ID to sync (for folder providers)

Config File (inbox/config/docflow_providers.yaml):
    providers:
      google_drive:
        enabled: true
        folder_id: "1abc123..."
        sync_extensions: [".docx", ".xlsx", ".pptx", ".gdoc"]
      dropbox:
        enabled: true
        folder_path: "/Neutron/Documents"
      box:
        enabled: false  # Not configured

Adding a New Provider:
1. Create a class extending DocProvider or FolderSyncProvider
2. Implement required methods
3. Register via: ProviderRegistry.register("slug", YourProvider)
4. Or use entry point: docflow.providers = your_module:YourProvider
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TypeVar

# Re-export core types for provider implementations
from axiom.infra.state import atomic_write

from ...models import Signal as Signal  # noqa: F401 — re-exported for provider implementations


class ProviderCapability(Enum):
    """Capabilities a provider may support."""
    FETCH_CONTENT = "fetch_content"       # Read document content
    EXTRACT_COMMENTS = "extract_comments"  # Extract review comments
    TRACKED_CHANGES = "tracked_changes"    # Extract track changes
    REVISION_HISTORY = "revision_history"  # Get version history
    FOLDER_LISTING = "folder_listing"      # List folder contents
    WATCH_CHANGES = "watch_changes"        # Real-time change notifications
    PUSH_CONTENT = "push_content"          # Write back to document
    CREATE_DOCUMENT = "create_document"    # Create new documents


@dataclass
class ProviderCredentials:
    """Standard credential container for providers."""
    access_token: str | None = None
    refresh_token: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    api_key: str | None = None
    expires_at: datetime | None = None

    @classmethod
    def from_env(cls, provider_slug: str) -> ProviderCredentials:
        """Load credentials from environment variables.

        Looks for: DOCFLOW_{SLUG}_TOKEN, DOCFLOW_{SLUG}_REFRESH, etc.
        """
        slug = provider_slug.upper()
        return cls(
            access_token=os.environ.get(f"DOCFLOW_{slug}_TOKEN"),
            refresh_token=os.environ.get(f"DOCFLOW_{slug}_REFRESH"),
            client_id=os.environ.get(f"DOCFLOW_{slug}_CLIENT_ID"),
            client_secret=os.environ.get(f"DOCFLOW_{slug}_SECRET"),
            api_key=os.environ.get(f"DOCFLOW_{slug}_API_KEY"),
        )

    @property
    def is_configured(self) -> bool:
        """Check if minimum credentials are available."""
        return bool(self.access_token or self.api_key)

    @property
    def needs_refresh(self) -> bool:
        """Check if token refresh is needed."""
        if not self.expires_at:
            return False
        return datetime.now(UTC) >= self.expires_at


class ChangeType(Enum):
    """Types of changes detected in external documents."""
    COMMENT = "comment"
    TRACKED_CHANGE = "tracked"
    CONTENT_DRIFT = "drift"
    STRUCTURAL = "structural"
    FILE_ADDED = "file_added"
    FILE_MODIFIED = "file_modified"
    FILE_DELETED = "file_deleted"


@dataclass
class ExternalChange:
    """A change detected in an external document or folder."""
    change_type: ChangeType
    path: str                     # File path or document section
    author: str = ""
    timestamp: str = ""
    original_text: str = ""
    new_text: str = ""
    comment_text: str = ""
    confidence: float = 0.9
    metadata: dict = field(default_factory=dict)


@dataclass
class SyncedFile:
    """Represents a file tracked by a folder sync provider."""
    local_path: Path              # Path in inbox/raw/docflow/{provider}/
    remote_id: str                # Provider-specific ID
    remote_path: str              # Path in remote system
    content_hash: str             # For change detection
    last_modified: str            # Remote modification time
    last_synced: str              # When we last synced
    metadata: dict = field(default_factory=dict)


class DocProvider(ABC):
    """Abstract base for single-document providers.

    Implementations: GoogleDocsProvider, BoxDocsProvider, MSGraphProvider

    Use for: Documents with stable URLs/IDs that are individually tracked.
    """

    # Override in subclass
    slug: str = "base"
    display_name: str = "Base Provider"
    capabilities: set[ProviderCapability] = set()

    def __init__(self, credentials: ProviderCredentials | None = None):
        self._credentials = credentials or ProviderCredentials.from_env(self.slug)

    @property
    def is_available(self) -> bool:
        """Check if provider is configured and ready."""
        return self._credentials.is_configured

    @abstractmethod
    def fetch_content(self, uri: str) -> tuple[str, list[ExternalChange]]:
        """Fetch document content and extract changes.

        Args:
            uri: Document URI (URL, ID, or path depending on provider)

        Returns:
            Tuple of (plain text content, list of changes/comments)
        """
        pass

    def get_revision_history(self, uri: str) -> list[dict]:
        """Get document revision history. Override if supported."""
        return []

    def extract_comments(self, uri: str) -> list[dict]:
        """Extract comments. Override if supported separately from fetch."""
        _, changes = self.fetch_content(uri)
        return [
            {"author": c.author, "text": c.comment_text, "timestamp": c.timestamp}
            for c in changes
            if c.change_type == ChangeType.COMMENT
        ]

    def push_content(self, uri: str, content: str) -> bool:
        """Push updated content back to document. Override if supported."""
        raise NotImplementedError(f"{self.display_name} does not support push")


class FolderSyncProvider(ABC):
    """Abstract base for folder-level sync providers.

    Implementations: DropboxProvider, GoogleDriveProvider, BoxFolderProvider

    Use for: Syncing entire folders of documents (like Dropbox or Google Drive).
    Handles incremental sync, change detection, and local caching.
    """

    # Override in subclass
    slug: str = "base_folder"
    display_name: str = "Base Folder Provider"
    capabilities: set[ProviderCapability] = set()
    supported_extensions: tuple[str, ...] = (".docx", ".xlsx", ".pptx", ".pdf")

    def __init__(
        self,
        credentials: ProviderCredentials | None = None,
        local_root: Path | None = None,
    ):
        self._credentials = credentials or ProviderCredentials.from_env(self.slug)
        # Local cache: inbox/raw/docflow/{slug}/
        from axiom import REPO_ROOT as _REPO_ROOT
        self._local_root = local_root or (
            _REPO_ROOT / "runtime" / "inbox" / "raw" / "docflow" / self.slug
        )

    @property
    def is_available(self) -> bool:
        """Check if provider is configured and ready."""
        return self._credentials.is_configured

    @abstractmethod
    def list_remote_files(self, folder_id: str | None = None) -> list[SyncedFile]:
        """List files in remote folder.

        Args:
            folder_id: Provider-specific folder ID. If None, use configured root.

        Returns:
            List of SyncedFile metadata (not content)
        """
        pass

    @abstractmethod
    def download_file(self, remote_id: str, local_path: Path) -> bool:
        """Download a single file to local cache.

        Args:
            remote_id: Provider-specific file ID
            local_path: Where to save locally

        Returns:
            True if download succeeded
        """
        pass

    def sync_folder(self, folder_id: str | None = None) -> list[ExternalChange]:
        """Perform incremental sync of folder.

        Returns:
            List of changes detected during sync
        """
        changes = []
        remote_files = self.list_remote_files(folder_id)

        # Load previous sync state
        state_file = self._local_root / ".sync_state.json"
        prev_state = {}
        if state_file.exists():
            import json
            prev_state = json.loads(state_file.read_text())

        for remote_file in remote_files:
            if not self._should_sync(remote_file):
                continue

            prev_hash = prev_state.get(remote_file.remote_id, {}).get("hash", "")

            if remote_file.content_hash != prev_hash:
                # File is new or modified
                local_path = self._local_root / remote_file.remote_path.lstrip("/")
                local_path.parent.mkdir(parents=True, exist_ok=True)

                if self.download_file(remote_file.remote_id, local_path):
                    change_type = (
                        ChangeType.FILE_ADDED if not prev_hash
                        else ChangeType.FILE_MODIFIED
                    )
                    changes.append(ExternalChange(
                        change_type=change_type,
                        path=str(local_path),
                        timestamp=remote_file.last_modified,
                        metadata={
                            "remote_id": remote_file.remote_id,
                            "remote_path": remote_file.remote_path,
                        },
                    ))

                    # Update state
                    prev_state[remote_file.remote_id] = {
                        "hash": remote_file.content_hash,
                        "local_path": str(local_path),
                        "synced_at": datetime.now(UTC).isoformat(),
                    }

        # Detect deletions
        remote_ids = {f.remote_id for f in remote_files}
        for remote_id, state in list(prev_state.items()):
            if remote_id not in remote_ids:
                changes.append(ExternalChange(
                    change_type=ChangeType.FILE_DELETED,
                    path=state.get("local_path", ""),
                    metadata={"remote_id": remote_id},
                ))
                del prev_state[remote_id]

        # Save updated state
        self._local_root.mkdir(parents=True, exist_ok=True)
        atomic_write(state_file, prev_state)

        return changes

    def _should_sync(self, file: SyncedFile) -> bool:
        """Check if file should be synced based on extension."""
        return any(file.remote_path.lower().endswith(ext) for ext in self.supported_extensions)


# =============================================================================
# Provider Registry
# =============================================================================

ProviderT = TypeVar("ProviderT", DocProvider, FolderSyncProvider)


class ProviderRegistry:
    """Registry for document and folder providers.

    Supports:
    - Explicit registration via register()
    - Entry point discovery (docflow.providers)
    - Lazy initialization of providers
    """

    _doc_providers: dict[str, type[DocProvider]] = {}
    _folder_providers: dict[str, type[FolderSyncProvider]] = {}
    _doc_instances: dict[str, DocProvider] = {}
    _folder_instances: dict[str, FolderSyncProvider] = {}

    @classmethod
    def register_doc_provider(
        cls,
        slug: str,
        provider_class: type[DocProvider],
    ) -> None:
        """Register a document provider class."""
        cls._doc_providers[slug] = provider_class

    @classmethod
    def register_folder_provider(
        cls,
        slug: str,
        provider_class: type[FolderSyncProvider],
    ) -> None:
        """Register a folder sync provider class."""
        cls._folder_providers[slug] = provider_class

    @classmethod
    def get_doc_provider(
        cls,
        slug: str,
        credentials: ProviderCredentials | None = None,
    ) -> DocProvider | None:
        """Get a document provider instance."""
        if slug not in cls._doc_providers:
            return None

        if slug not in cls._doc_instances:
            cls._doc_instances[slug] = cls._doc_providers[slug](credentials)
        return cls._doc_instances[slug]

    @classmethod
    def get_folder_provider(
        cls,
        slug: str,
        credentials: ProviderCredentials | None = None,
    ) -> FolderSyncProvider | None:
        """Get a folder provider instance."""
        if slug not in cls._folder_providers:
            return None

        if slug not in cls._folder_instances:
            cls._folder_instances[slug] = cls._folder_providers[slug](credentials)
        return cls._folder_instances[slug]

    @classmethod
    def list_doc_providers(cls) -> list[str]:
        """List registered document provider slugs."""
        return list(cls._doc_providers.keys())

    @classmethod
    def list_folder_providers(cls) -> list[str]:
        """List registered folder provider slugs."""
        return list(cls._folder_providers.keys())

    @classmethod
    def list_available_providers(cls) -> dict[str, list[str]]:
        """List all configured and ready providers."""
        available = {"doc": [], "folder": []}

        for slug in cls._doc_providers:
            provider = cls.get_doc_provider(slug)
            if provider and provider.is_available:
                available["doc"].append(slug)

        for slug in cls._folder_providers:
            provider = cls.get_folder_provider(slug)
            if provider and provider.is_available:
                available["folder"].append(slug)

        return available

    @classmethod
    def discover_entry_points(cls) -> None:
        """Discover providers via setuptools entry points.

        Entry point group: docflow.providers
        Expected format: slug = module:ProviderClass
        """
        try:
            from importlib.metadata import entry_points

            eps = entry_points()
            provider_eps: list = []

            if hasattr(eps, "select"):
                # Python 3.10+
                provider_eps = list(eps.select(group="docflow.providers"))
            elif isinstance(eps, dict):
                # Python 3.9 dict-like
                provider_eps = eps.get("docflow.providers", [])

            for ep in provider_eps:
                try:
                    provider_class = ep.load()
                    if issubclass(provider_class, DocProvider):
                        cls.register_doc_provider(ep.name, provider_class)
                    elif issubclass(provider_class, FolderSyncProvider):
                        cls.register_folder_provider(ep.name, provider_class)
                except Exception:
                    pass  # Skip broken entry points
        except ImportError:
            pass  # No entry points support


# =============================================================================
# Built-in Provider Stubs (for documentation and extension)
# =============================================================================

class GoogleDocsProvider(DocProvider):
    """Provider for Google Docs via Google Drive API.

    Setup:
        1. Create OAuth credentials in Google Cloud Console
        2. Set environment variables:
           - DOCFLOW_GOOGLE_CLIENT_ID
           - DOCFLOW_GOOGLE_SECRET
           - DOCFLOW_GOOGLE_REFRESH (after OAuth flow)

    Document URIs:
        - Google Doc ID: "1abc123def456..."
        - Google Drive URL: "https://docs.google.com/document/d/1abc123..."
    """

    slug = "google_docs"
    display_name = "Google Docs"
    capabilities = {
        ProviderCapability.FETCH_CONTENT,
        ProviderCapability.EXTRACT_COMMENTS,
        ProviderCapability.REVISION_HISTORY,
    }

    def __init__(self, credentials: ProviderCredentials | None = None):
        super().__init__(credentials)
        self._base_url = "https://www.googleapis.com"

    def fetch_content(self, uri: str) -> tuple[str, list[ExternalChange]]:
        """Fetch Google Doc content and comments."""
        if not self.is_available:
            raise ValueError("Google Docs credentials not configured")

        import requests

        doc_id = self._extract_doc_id(uri)
        headers = {"Authorization": f"Bearer {self._credentials.access_token}"}
        changes = []

        # Export as plain text
        export_url = f"{self._base_url}/drive/v3/files/{doc_id}/export"
        params = {"mimeType": "text/plain"}
        resp = requests.get(export_url, headers=headers, params=params)

        if not resp.ok:
            raise RuntimeError(f"Failed to fetch document: {resp.status_code}")

        content = resp.text

        # Get comments via Drive API
        comments_url = f"{self._base_url}/drive/v3/files/{doc_id}/comments"
        params = {"fields": "comments(id,content,author,createdTime,resolved)"}
        resp = requests.get(comments_url, headers=headers, params=params)

        if resp.ok:
            for comment in resp.json().get("comments", []):
                if not comment.get("resolved", False):
                    changes.append(ExternalChange(
                        change_type=ChangeType.COMMENT,
                        path="",
                        author=comment.get("author", {}).get("displayName", "Unknown"),
                        timestamp=comment.get("createdTime", ""),
                        comment_text=comment.get("content", ""),
                        confidence=0.95,
                    ))

        return content, changes

    def get_revision_history(self, uri: str) -> list[dict]:
        """Get Google Doc revision history."""
        if not self.is_available:
            return []

        import requests

        doc_id = self._extract_doc_id(uri)
        headers = {"Authorization": f"Bearer {self._credentials.access_token}"}

        url = f"{self._base_url}/drive/v3/files/{doc_id}/revisions"
        resp = requests.get(url, headers=headers)

        if resp.ok:
            return resp.json().get("revisions", [])
        return []

    def _extract_doc_id(self, uri: str) -> str:
        """Extract document ID from URL or return as-is if already an ID."""
        if uri.startswith("https://docs.google.com/document/d/"):
            # URL format: https://docs.google.com/document/d/{id}/...
            parts = uri.split("/d/")[1].split("/")
            return parts[0]
        return uri


class GoogleDriveProvider(FolderSyncProvider):
    """Provider for syncing Google Drive folders.

    Setup:
        1. Create OAuth credentials in Google Cloud Console
        2. Set environment variables:
           - DOCFLOW_GOOGLE_DRIVE_TOKEN
           - DOCFLOW_GOOGLE_DRIVE_FOLDER_ID (root folder to sync)

    Syncs .docx, .xlsx, .pptx, and Google-native docs (exported).
    """

    slug = "google_drive"
    display_name = "Google Drive"
    capabilities = {
        ProviderCapability.FOLDER_LISTING,
        ProviderCapability.FETCH_CONTENT,
    }
    supported_extensions = (".docx", ".xlsx", ".pptx", ".pdf", ".gdoc", ".gsheet")

    def __init__(
        self,
        credentials: ProviderCredentials | None = None,
        local_root: Path | None = None,
    ):
        super().__init__(credentials, local_root)
        self._base_url = "https://www.googleapis.com/drive/v3"

    def list_remote_files(self, folder_id: str | None = None) -> list[SyncedFile]:
        """List files in Google Drive folder."""
        if not self.is_available:
            return []

        import requests

        folder_id = folder_id or os.environ.get("DOCFLOW_GOOGLE_DRIVE_FOLDER_ID")
        if not folder_id:
            return []

        headers = {"Authorization": f"Bearer {self._credentials.access_token}"}
        files = []

        # Query files in folder
        query = f"'{folder_id}' in parents and trashed=false"
        url = f"{self._base_url}/files"
        params = {
            "q": query,
            "fields": "files(id,name,mimeType,modifiedTime,md5Checksum)",
        }

        resp = requests.get(url, headers=headers, params=params)
        if resp.ok:
            for f in resp.json().get("files", []):
                files.append(SyncedFile(
                    local_path=self._local_root / f["name"],
                    remote_id=f["id"],
                    remote_path=f["name"],
                    content_hash=f.get("md5Checksum", f["id"]),
                    last_modified=f.get("modifiedTime", ""),
                    last_synced="",
                    metadata={"mimeType": f.get("mimeType")},
                ))

        return files

    def download_file(self, remote_id: str, local_path: Path) -> bool:
        """Download file from Google Drive."""
        if not self.is_available:
            return False

        import requests

        headers = {"Authorization": f"Bearer {self._credentials.access_token}"}

        # Check mime type for export vs download
        meta_url = f"{self._base_url}/files/{remote_id}"
        meta_resp = requests.get(meta_url, headers=headers, params={"fields": "mimeType"})

        if not meta_resp.ok:
            return False

        mime_type = meta_resp.json().get("mimeType", "")

        if mime_type.startswith("application/vnd.google-apps"):
            # Google-native format, need to export
            export_mime = {
                "application/vnd.google-apps.document": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.google-apps.spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.google-apps.presentation": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            }.get(mime_type)

            if not export_mime:
                return False

            url = f"{self._base_url}/files/{remote_id}/export"
            resp = requests.get(url, headers=headers, params={"mimeType": export_mime})
        else:
            # Binary file, direct download
            url = f"{self._base_url}/files/{remote_id}"
            resp = requests.get(url, headers=headers, params={"alt": "media"})

        if resp.ok:
            local_path.write_bytes(resp.content)
            return True
        return False


class BoxProvider(DocProvider):
    """Provider for Box documents via Box API.

    Setup:
        1. Create Box application with OAuth 2.0
        2. Set environment variables:
           - DOCFLOW_BOX_TOKEN

    Document URIs:
        - Box file ID: "123456789"
        - Box URL: "https://app.box.com/file/123456789"
    """

    slug = "box"
    display_name = "Box"
    capabilities = {
        ProviderCapability.FETCH_CONTENT,
        ProviderCapability.EXTRACT_COMMENTS,
        ProviderCapability.REVISION_HISTORY,
    }

    def __init__(self, credentials: ProviderCredentials | None = None):
        super().__init__(credentials)
        self._base_url = "https://api.box.com/2.0"

    def fetch_content(self, uri: str) -> tuple[str, list[ExternalChange]]:
        """Fetch Box document content and comments."""
        if not self.is_available:
            raise ValueError("Box credentials not configured")

        import io

        import requests

        file_id = self._extract_file_id(uri)
        headers = {"Authorization": f"Bearer {self._credentials.access_token}"}
        changes = []

        # Get file info
        info_url = f"{self._base_url}/files/{file_id}"
        info_resp = requests.get(info_url, headers=headers)

        if not info_resp.ok:
            raise RuntimeError(f"Failed to fetch file info: {info_resp.status_code}")

        file_info = info_resp.json()

        # Download content
        content_url = f"{self._base_url}/files/{file_id}/content"
        resp = requests.get(content_url, headers=headers)

        if not resp.ok:
            raise RuntimeError(f"Failed to download file: {resp.status_code}")

        # Parse content based on type
        name = file_info.get("name", "")
        if name.endswith(".docx"):
            try:
                import docx
                doc = docx.Document(io.BytesIO(resp.content))
                content = "\n".join(p.text for p in doc.paragraphs)
            except ImportError:
                content = "[Binary .docx - python-docx required]"
        else:
            content = resp.text

        # Get comments
        comments_url = f"{self._base_url}/files/{file_id}/comments"
        comments_resp = requests.get(comments_url, headers=headers)

        if comments_resp.ok:
            for comment in comments_resp.json().get("entries", []):
                changes.append(ExternalChange(
                    change_type=ChangeType.COMMENT,
                    path="",
                    author=comment.get("created_by", {}).get("name", "Unknown"),
                    timestamp=comment.get("created_at", ""),
                    comment_text=comment.get("message", ""),
                    confidence=0.95,
                ))

        return content, changes

    def get_revision_history(self, uri: str) -> list[dict]:
        """Get Box file version history."""
        if not self.is_available:
            return []

        import requests

        file_id = self._extract_file_id(uri)
        headers = {"Authorization": f"Bearer {self._credentials.access_token}"}

        url = f"{self._base_url}/files/{file_id}/versions"
        resp = requests.get(url, headers=headers)

        if resp.ok:
            return resp.json().get("entries", [])
        return []

    def _extract_file_id(self, uri: str) -> str:
        """Extract file ID from URL or return as-is."""
        if "box.com" in uri:
            # URL formats:
            # https://app.box.com/file/123456789
            # https://app.box.com/s/abc123/file/123456789
            parts = uri.split("/file/")
            if len(parts) > 1:
                return parts[1].split("/")[0].split("?")[0]
        return uri


class DropboxProvider(FolderSyncProvider):
    """Provider for syncing Dropbox folders.

    Setup:
        1. Create Dropbox app at https://www.dropbox.com/developers/apps
        2. Set environment variables:
           - DOCFLOW_DROPBOX_TOKEN (access token)
           - DOCFLOW_DROPBOX_FOLDER_PATH (e.g., "/Neutron/Documents")
    """

    slug = "dropbox"
    display_name = "Dropbox"
    capabilities = {
        ProviderCapability.FOLDER_LISTING,
        ProviderCapability.FETCH_CONTENT,
        ProviderCapability.WATCH_CHANGES,
    }
    supported_extensions = (".docx", ".xlsx", ".pptx", ".pdf", ".txt")

    def __init__(
        self,
        credentials: ProviderCredentials | None = None,
        local_root: Path | None = None,
    ):
        super().__init__(credentials, local_root)
        self._base_url = "https://api.dropboxapi.com/2"
        self._content_url = "https://content.dropboxapi.com/2"

    def list_remote_files(self, folder_id: str | None = None) -> list[SyncedFile]:
        """List files in Dropbox folder."""
        if not self.is_available:
            return []

        import requests

        folder_path = folder_id or os.environ.get("DOCFLOW_DROPBOX_FOLDER_PATH", "")
        if not folder_path:
            return []

        headers = {
            "Authorization": f"Bearer {self._credentials.access_token}",
            "Content-Type": "application/json",
        }
        files = []

        url = f"{self._base_url}/files/list_folder"
        data = {
            "path": folder_path,
            "recursive": False,
            "include_deleted": False,
        }

        resp = requests.post(url, headers=headers, json=data)
        if resp.ok:
            for entry in resp.json().get("entries", []):
                if entry[".tag"] == "file":
                    files.append(SyncedFile(
                        local_path=self._local_root / entry["name"],
                        remote_id=entry["id"],
                        remote_path=entry["path_display"],
                        content_hash=entry.get("content_hash", entry["id"]),
                        last_modified=entry.get("server_modified", ""),
                        last_synced="",
                    ))

        return files

    def download_file(self, remote_id: str, local_path: Path) -> bool:
        """Download file from Dropbox."""
        if not self.is_available:
            return False

        import json

        import requests

        headers = {
            "Authorization": f"Bearer {self._credentials.access_token}",
            "Dropbox-API-Arg": json.dumps({"path": remote_id}),
        }

        url = f"{self._content_url}/files/download"
        resp = requests.post(url, headers=headers)

        if resp.ok:
            local_path.write_bytes(resp.content)
            return True
        return False


# =============================================================================
# Register Built-in Providers
# =============================================================================

ProviderRegistry.register_doc_provider("google_docs", GoogleDocsProvider)
ProviderRegistry.register_doc_provider("box", BoxProvider)
ProviderRegistry.register_folder_provider("google_drive", GoogleDriveProvider)
ProviderRegistry.register_folder_provider("dropbox", DropboxProvider)

# Discover additional providers from entry points
ProviderRegistry.discover_entry_points()


# =============================================================================
# Convenience Functions
# =============================================================================

def list_providers() -> dict[str, list[str]]:
    """List all available (configured) providers."""
    return ProviderRegistry.list_available_providers()


def sync_all_folders() -> list[ExternalChange]:
    """Sync all configured folder providers."""
    changes = []
    for slug in ProviderRegistry.list_folder_providers():
        provider = ProviderRegistry.get_folder_provider(slug)
        if provider and provider.is_available:
            changes.extend(provider.sync_folder())
    return changes


def fetch_doc(provider_slug: str, uri: str) -> tuple[str, list[ExternalChange]]:
    """Fetch document from specified provider."""
    provider = ProviderRegistry.get_doc_provider(provider_slug)
    if not provider:
        raise ValueError(f"Unknown provider: {provider_slug}")
    if not provider.is_available:
        raise ValueError(f"Provider {provider_slug} not configured")
    return provider.fetch_content(uri)
