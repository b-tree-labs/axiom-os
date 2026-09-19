# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""OneDrive Graph API Storage — uploads files via Microsoft Graph API.

Uses device code flow for authentication — works with any Microsoft
account (personal, org, edu) without developer app registration.

First run: displays a code to enter at microsoft.com/devicelogin.
After auth, tokens are cached and refreshed automatically.

Usage:
    axi pub push --storage onedrive-graph --all

Requires: no extra dependencies (uses stdlib urllib only).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from axiom.infra.branding import get_branding as _get_branding
from axiom.infra.paths import get_user_state_dir

from ..base import StorageEntry, StorageProvider, UploadResult

logger = logging.getLogger(__name__)


def _get_token_dir() -> Path:
    return get_user_state_dir() / "credentials" / "microsoft"


# Microsoft's well-known public client ID for device code flow.
# This is the "Microsoft Office" public client — works for any tenant
# without app registration.
_PUBLIC_CLIENT_ID = "d3590ed6-52b1-4102-aeff-aad2292ab01c"

# Scopes needed for OneDrive file upload
_SCOPES = "Files.ReadWrite.All offline_access"


class OneDriveGraphStorageProvider(StorageProvider):
    """OneDrive storage via Microsoft Graph API with device code auth."""

    GRAPH_BASE = "https://graph.microsoft.com/v1.0"

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}
        self.token_dir = Path(config.get("token_dir", str(_get_token_dir())))
        self.target_folder = config.get("folder", _get_branding().product_name)
        self.client_id = config.get("client_id", _PUBLIC_CLIENT_ID)

        self._access_token: str = ""
        self._token_expiry: datetime | None = None

    def is_available(self) -> bool:
        return True  # No extra dependencies needed

    def has_session(self) -> bool:
        return (self.token_dir / "token.json").exists()

    def _get_token(self) -> str:
        """Get a valid access token, refreshing if needed."""
        # Check in-memory cache
        if self._access_token != "" and self._token_expiry:
            if datetime.now(UTC) < self._token_expiry:
                return self._access_token

        # Try loading from file
        token_file = self.token_dir / "token.json"
        if token_file.exists():
            try:
                data = json.loads(token_file.read_text())
                expiry = datetime.fromisoformat(data["expires_at"])
                if datetime.now(UTC) < expiry:
                    self._access_token = data["access_token"]
                    self._token_expiry = expiry
                    return self._access_token
                # Token expired — try refresh
                if data.get("refresh_token"):
                    return self._refresh_token(data["refresh_token"])
            except Exception:
                pass

        # Need fresh authentication
        return self._device_code_auth()

    def _device_code_auth(self) -> str:
        """Authenticate via device code flow."""
        # Determine tenant from user settings
        tenant = "organizations"
        try:
            from axiom.extensions.builtins.settings.store import SettingsStore

            org_tenant = SettingsStore().get("user.org_tenant", "")
            if org_tenant:
                tenant = org_tenant
        except Exception:
            pass

        # Start device code flow
        device_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode"
        data = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "scope": _SCOPES,
            }
        ).encode()

        req = urllib.request.Request(device_url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            device_resp = json.loads(resp.read())

        # Display instructions to user
        print(f"\n  To authenticate, visit: {device_resp['verification_uri']}")
        print(f"  Enter code: {device_resp['user_code']}")
        print("  Waiting for authentication...\n")

        # Poll for token
        token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        interval = device_resp.get("interval", 5)
        expires_in = device_resp.get("expires_in", 900)
        deadline = datetime.now() + timedelta(seconds=expires_in)

        while datetime.now() < deadline:
            time.sleep(interval)

            poll_data = urllib.parse.urlencode(
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": self.client_id,
                    "device_code": device_resp["device_code"],
                }
            ).encode()

            poll_req = urllib.request.Request(token_url, data=poll_data, method="POST")

            try:
                with urllib.request.urlopen(poll_req, timeout=30) as resp:
                    token_resp = json.loads(resp.read())

                self._access_token = token_resp["access_token"]
                expires_in_secs = token_resp.get("expires_in", 3600)
                self._token_expiry = datetime.now(UTC) + timedelta(seconds=expires_in_secs - 60)

                # Save token
                self.token_dir.mkdir(parents=True, exist_ok=True)
                token_data = {
                    "access_token": self._access_token,
                    "refresh_token": token_resp.get("refresh_token", ""),
                    "expires_at": self._token_expiry.isoformat(),
                }
                token_file = self.token_dir / "token.json"
                token_file.write_text(json.dumps(token_data, indent=2))
                os.chmod(str(token_file), 0o600)

                print("  ✓ Authentication successful — token saved.\n")
                return self._access_token

            except urllib.error.HTTPError as e:
                error_body = json.loads(e.read().decode())
                error_code = error_body.get("error")
                if error_code == "authorization_pending":
                    continue
                if error_code == "slow_down":
                    interval += 5
                    continue
                raise RuntimeError(
                    f"Auth failed: {error_body.get('error_description', error_code)}"
                )

        raise RuntimeError("Device code expired. Please try again.")

    def _refresh_token(self, refresh_token: str) -> str:
        """Refresh an expired access token."""
        tenant = "organizations"
        try:
            from axiom.extensions.builtins.settings.store import SettingsStore

            org_tenant = SettingsStore().get("user.org_tenant", "")
            if org_tenant:
                tenant = org_tenant
        except Exception:
            pass

        token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        data = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": refresh_token,
                "scope": _SCOPES,
            }
        ).encode()

        req = urllib.request.Request(token_url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                token_resp = json.loads(resp.read())

            self._access_token = token_resp["access_token"]
            expires_in = token_resp.get("expires_in", 3600)
            self._token_expiry = datetime.now(UTC) + timedelta(seconds=expires_in - 60)

            # Save updated token
            token_data = {
                "access_token": self._access_token,
                "refresh_token": token_resp.get("refresh_token", refresh_token),
                "expires_at": self._token_expiry.isoformat(),
            }
            token_file = self.token_dir / "token.json"
            token_file.write_text(json.dumps(token_data, indent=2))
            os.chmod(str(token_file), 0o600)

            return self._access_token
        except Exception:
            # Refresh failed — need re-auth
            return self._device_code_auth()

    def _graph_request(
        self,
        endpoint: str,
        method: str = "GET",
        data: bytes | None = None,
        content_type: str = "application/json",
    ) -> dict:
        """Make an authenticated Graph API request."""
        token = self._get_token()
        url = f"{self.GRAPH_BASE}{endpoint}"
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())

    def _ensure_folder(self, folder_path: str) -> str:
        """Ensure folder exists on OneDrive, return folder ID."""
        parts = [p for p in folder_path.strip("/").split("/") if p]
        parent_path = ""

        for part in parts:
            # Check if folder exists
            check_path = (
                f"/me/drive/root:/{parent_path}/{part}"
                if parent_path
                else f"/me/drive/root:/{part}"
            )
            try:
                self._graph_request(check_path)
                parent_path = f"{parent_path}/{part}" if parent_path else part
                continue
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise

            # Create folder
            create_path = (
                f"/me/drive/root:/{parent_path}:/children"
                if parent_path
                else "/me/drive/root/children"
            )
            create_data = json.dumps(
                {
                    "name": part,
                    "folder": {},
                    "@microsoft.graph.conflictBehavior": "fail",
                }
            ).encode()

            try:
                self._graph_request(create_path, method="POST", data=create_data)
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    pass  # Already exists
                else:
                    raise

            parent_path = f"{parent_path}/{part}" if parent_path else part

        return parent_path

    def upload(
        self,
        local_path: Path,
        destination: str | None = None,
        metadata: dict | None = None,
        *,
        draft: bool = False,
        headed: bool = False,
    ) -> UploadResult:
        if not local_path.exists():
            return UploadResult(success=False, url="", error=f"File not found: {local_path}")

        remote_name = destination or local_path.name
        folder_path = self.target_folder

        try:
            # Ensure folder exists
            self._ensure_folder(folder_path)

            # Upload file (simple upload for < 4MB, session upload for larger)
            file_bytes = local_path.read_bytes()
            upload_path = f"/me/drive/root:/{folder_path}/{remote_name}:/content"

            token = self._get_token()
            url = f"{self.GRAPH_BASE}{upload_path}"
            req = urllib.request.Request(
                url,
                data=file_bytes,
                method="PUT",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read())

            web_url = result.get("webUrl", "")
            item_id = result.get("id", "")

            return UploadResult(
                success=True,
                url=web_url,
                storage_id=item_id,
                metadata={
                    "folder": folder_path,
                    "name": remote_name,
                    "method": "graph_api",
                    "size": len(file_bytes),
                },
            )

        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode()[:500]
            except Exception:
                pass
            return UploadResult(
                success=False,
                url="",
                error=f"Graph API error ({e.code}): {error_body}",
            )
        except Exception as e:
            return UploadResult(success=False, url="", error=str(e))

    def upload_to_folder(
        self,
        local_path: Path,
        folder_path: str,
        remote_name: str | None = None,
    ) -> UploadResult:
        """Upload a file to a specific folder path."""
        if not local_path.exists():
            return UploadResult(success=False, url="", error=f"File not found: {local_path}")

        remote_name = remote_name or local_path.name

        try:
            self._ensure_folder(folder_path)
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode()[:500]
            except Exception:
                pass
            return UploadResult(
                success=False,
                url="",
                error=f"Folder creation failed ({e.code}): {error_body}",
            )
        except Exception as e:
            return UploadResult(success=False, url="", error=f"Folder creation failed: {e}")

        try:
            file_bytes = local_path.read_bytes()
            upload_path = f"/me/drive/root:/{folder_path}/{remote_name}:/content"

            token = self._get_token()
            url = f"{self.GRAPH_BASE}{upload_path}"
            req = urllib.request.Request(
                url,
                data=file_bytes,
                method="PUT",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read())

            return UploadResult(
                success=True,
                url=result.get("webUrl", ""),
                storage_id=result.get("id", ""),
                metadata={
                    "folder": folder_path,
                    "name": remote_name,
                    "method": "graph_api",
                    "size": len(file_bytes),
                },
            )
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode()[:500]
            except Exception:
                pass
            return UploadResult(
                success=False,
                url="",
                error=f"Upload failed ({e.code}): {error_body}",
            )
        except Exception as e:
            return UploadResult(success=False, url="", error=f"Upload failed: {e}")

    def upload_batch(
        self,
        files: list[Path],
        *,
        draft: bool = False,
        headed: bool = False,
        folders: list[str] | None = None,
    ) -> list[UploadResult]:
        """Upload multiple files. Auth happens once, then batch uploads.

        Args:
            files: List of local file paths
            folders: Optional per-file folder paths (same length as files).
                     If None, uses self.target_folder for all.
        """
        results: list[UploadResult] = []

        # Authenticate upfront
        try:
            self._get_token()
        except Exception as e:
            return [UploadResult(success=False, url="", error=str(e)) for _ in files]

        if folders is None:
            folders = [self.target_folder] * len(files)

        for i, (local_path, folder) in enumerate(zip(files, folders)):
            logger.debug("Upload: %s → %s/%s", local_path, folder, local_path.name)
            print(f"    [{i + 1}/{len(files)}] {folder}/{local_path.name}...", end=" ", flush=True)
            result = self.upload_to_folder(local_path, folder)
            results.append(result)
            if result.success:
                print("✓", flush=True)
            else:
                print(f"✗ {result.error[:80]}", flush=True)
                if i == 0:
                    print("\n    First upload failed. Stopping batch.", flush=True)
                    for _ in files[i + 1 :]:
                        results.append(UploadResult(success=False, url="", error="Skipped"))
                    break

        return results

    def clear_session(self) -> None:
        token_file = self.token_dir / "token.json"
        if token_file.exists():
            token_file.unlink()

    # StorageProvider interface
    def list_files(self, folder: str = "") -> list[StorageEntry]:
        return []

    def list_artifacts(self, folder: str = "") -> list[StorageEntry] | list[dict]:
        return []

    def download(self, storage_id: str, local_path: Path) -> Path | bool:
        return False

    def delete(self, storage_id: str) -> bool:
        return False

    def move(self, source: str, destination: str) -> UploadResult | bool:
        return False

    def get_canonical_url(self, storage_id: str) -> str:
        return ""
