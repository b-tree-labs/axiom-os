# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Box Browser Storage — uploads/downloads files via Playwright + Box REST API.

Same pattern as OneDrive browser connector:
1. Playwright opens app.box.com
2. User does SSO + MFA (first time only)
3. JavaScript in page context calls Box API using authenticated session
4. No developer API keys needed

Session cookies stored at ~/<state-dir>/credentials/box/ (user-scoped).

Usage:
    # First time (opens browser for login)
    axi pub push --storage box-browser --headed docs/my-doc.docx

    # Subsequent runs (headless)
    axi pub push --storage box-browser docs/my-doc.docx

Requires: pip install playwright && playwright install chromium
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from axiom.infra.branding import get_branding as _get_branding
from axiom.infra.paths import get_user_state_dir

from ...factory import PublisherFactory
from ..base import StorageEntry, StorageProvider, UploadResult

logger = logging.getLogger(__name__)


def _get_session_dir() -> Path:
    return get_user_state_dir() / "credentials" / "box"


class BoxBrowserStorageProvider(StorageProvider):
    """Box storage via Playwright browser automation + Box REST API."""

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}
        self.session_dir = Path(
            config.get("session_dir")
            or os.environ.get("NEUT_BOX_SESSION_DIR")
            or str(_get_session_dir())
        )
        self.target_folder = config.get("folder", _get_branding().product_name)
        self.headless = config.get("headless", True)

    @staticmethod
    def _ensure_playwright():
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "Playwright not installed. Run:\n"
                "  pip install playwright && playwright install chromium"
            )

    def has_session(self) -> bool:
        return (self.session_dir / "state.json").exists()

    def is_available(self) -> bool:
        try:
            import playwright  # noqa: F401

            return True
        except ImportError:
            return False

    def upload(
        self,
        local_path: Path,
        destination: str | None = None,
        metadata: dict | None = None,
        *,
        draft: bool = False,
        headed: bool = False,
    ) -> UploadResult:
        self._ensure_playwright()
        from playwright.sync_api import sync_playwright

        if not local_path.exists():
            return UploadResult(success=False, url="", error=f"File not found: {local_path}")

        remote_name = destination or local_path.name
        headless = not headed and self.headless
        if not self.has_session() and headless:
            headless = False

        self.session_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(
                storage_state=str(self.session_dir / "state.json") if self.has_session() else None,
            )
            page = context.new_page()

            try:
                result = self._upload_to_box(page, context, local_path, remote_name)
                context.storage_state(path=str(self.session_dir / "state.json"))
                os.chmod(str(self.session_dir / "state.json"), 0o600)
                return result
            except Exception as e:
                try:
                    context.storage_state(path=str(self.session_dir / "state.json"))
                    os.chmod(str(self.session_dir / "state.json"), 0o600)
                except Exception:
                    pass
                return UploadResult(success=False, url="", error=str(e))
            finally:
                context.close()
                browser.close()

    def upload_batch(
        self,
        files: list[Path],
        *,
        draft: bool = False,
        headed: bool = False,
        folders: list[str] | None = None,
    ) -> list[UploadResult]:
        self._ensure_playwright()
        from playwright.sync_api import sync_playwright

        headless = not headed and self.headless
        if not self.has_session() and headless:
            headless = False

        self.session_dir.mkdir(parents=True, exist_ok=True)
        results: list[UploadResult] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(
                storage_state=str(self.session_dir / "state.json") if self.has_session() else None,
            )
            page = context.new_page()

            try:
                # Navigate to Box and authenticate once
                page.goto("https://app.box.com", wait_until="domcontentloaded", timeout=30000)

                if self._needs_login(page):
                    self._do_login(page)

                page.wait_for_timeout(3000)
                context.storage_state(path=str(self.session_dir / "state.json"))
                os.chmod(str(self.session_dir / "state.json"), 0o600)

                for i, local_path in enumerate(files):
                    if not local_path.exists():
                        results.append(
                            UploadResult(
                                success=False,
                                url="",
                                error=f"File not found: {local_path}",
                            )
                        )
                        print(
                            f"    [{i + 1}/{len(files)}] ✗ {local_path.name} — file not found",
                            flush=True,
                        )
                        continue

                    print(
                        f"    [{i + 1}/{len(files)}] Uploading {local_path.name}...",
                        end=" ",
                        flush=True,
                    )
                    result = self._upload_to_box(page, context, local_path, local_path.name)
                    results.append(result)
                    if result.success:
                        print("✓", flush=True)
                    else:
                        print(f"✗ {result.error[:80]}", flush=True)
                        if i == 0:
                            print("\n    First upload failed. Stopping batch.", flush=True)
                            for _remaining in files[i + 1 :]:
                                results.append(
                                    UploadResult(
                                        success=False,
                                        url="",
                                        error="Skipped (first upload failed)",
                                    )
                                )
                            break

                context.storage_state(path=str(self.session_dir / "state.json"))
                os.chmod(str(self.session_dir / "state.json"), 0o600)

            except Exception as e:
                try:
                    context.storage_state(path=str(self.session_dir / "state.json"))
                    os.chmod(str(self.session_dir / "state.json"), 0o600)
                except Exception:
                    pass
                while len(results) < len(files):
                    results.append(
                        UploadResult(
                            success=False,
                            url="",
                            error=f"Session error: {e}",
                        )
                    )
            finally:
                context.close()
                browser.close()

        return results

    def _needs_login(self, page) -> bool:
        url = page.url
        return "account.box.com" in url or "login" in url.lower() or "sso" in url.lower()

    def _do_login(self, page) -> None:
        if self.headless:
            raise RuntimeError("Login required but running headless. Run with --headed first.")
        print("\n  Box login required.")
        print("  Complete login + MFA in the browser window.\n")
        try:
            page.wait_for_url(
                re.compile(r"app\.box\.com"),
                timeout=300_000,
            )
            print("  ✓ Login successful.\n")
        except Exception:
            raise RuntimeError("Login timed out (5 minute limit).")

    def _upload_to_box(
        self,
        page,
        context,
        local_path: Path,
        remote_name: str,
    ) -> UploadResult:
        """Upload a file using Box API via the browser's authenticated session."""
        import base64

        file_bytes = local_path.read_bytes()
        file_size = len(file_bytes)
        b64_content = base64.b64encode(file_bytes).decode("ascii")
        folder_name = self.target_folder

        # Use Box API from within the authenticated browser context
        upload_result = page.evaluate(
            """async (args) => {
            try {
                // Step 1: Find or create the target folder
                // Search for folder by name
                const searchResp = await fetch(
                    'https://api.box.com/2.0/search?query=' +
                    encodeURIComponent(args.folderName) +
                    '&type=folder&limit=5',
                    { headers: { 'Accept': 'application/json' } }
                );

                let folderId = '0'; // Default: root folder
                if (searchResp.ok) {
                    const searchData = await searchResp.json();
                    const match = searchData.entries?.find(
                        e => e.name === args.folderName && e.type === 'folder'
                    );
                    if (match) {
                        folderId = match.id;
                    } else {
                        // Create the folder
                        const createResp = await fetch('https://api.box.com/2.0/folders', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'Accept': 'application/json',
                            },
                            body: JSON.stringify({
                                name: args.folderName,
                                parent: { id: '0' }
                            })
                        });
                        if (createResp.ok) {
                            const created = await createResp.json();
                            folderId = created.id;
                        } else if (createResp.status === 409) {
                            // Folder already exists — extract ID from conflict response
                            const conflict = await createResp.json();
                            folderId = conflict.context_info?.conflicts?.[0]?.id || '0';
                        }
                    }
                }

                // Step 2: Check if file already exists (for overwrite)
                const listResp = await fetch(
                    'https://api.box.com/2.0/folders/' + folderId + '/items?limit=100',
                    { headers: { 'Accept': 'application/json' } }
                );
                let existingFileId = null;
                if (listResp.ok) {
                    const listData = await listResp.json();
                    const existing = listData.entries?.find(
                        e => e.name === args.fileName && e.type === 'file'
                    );
                    if (existing) existingFileId = existing.id;
                }

                // Step 3: Upload (new) or upload new version (existing)
                const binaryString = atob(args.b64);
                const bytes = new Uint8Array(binaryString.length);
                for (let i = 0; i < binaryString.length; i++) {
                    bytes[i] = binaryString.charCodeAt(i);
                }

                const formData = new FormData();
                const blob = new Blob([bytes.buffer], {
                    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                });

                let uploadUrl, method;
                if (existingFileId) {
                    // Upload new version
                    uploadUrl = 'https://upload.box.com/api/2.0/files/' + existingFileId + '/content';
                    formData.append('file', blob, args.fileName);
                } else {
                    // New upload
                    uploadUrl = 'https://upload.box.com/api/2.0/files/content';
                    formData.append('attributes', JSON.stringify({
                        name: args.fileName,
                        parent: { id: folderId }
                    }));
                    formData.append('file', blob, args.fileName);
                }

                const uploadResp = await fetch(uploadUrl, {
                    method: 'POST',
                    body: formData,
                });

                if (!uploadResp.ok) {
                    const errText = await uploadResp.text();
                    return {
                        success: false,
                        error: uploadResp.status + ': ' + errText.substring(0, 300)
                    };
                }

                const result = await uploadResp.json();
                const file = result.entries?.[0] || {};
                return {
                    success: true,
                    url: 'https://app.box.com/file/' + (file.id || ''),
                    fileId: file.id || '',
                    name: file.name || args.fileName,
                };
            } catch(e) {
                return { success: false, error: e.toString() };
            }
        }""",
            {
                "b64": b64_content,
                "folderName": folder_name,
                "fileName": remote_name,
            },
        )

        if upload_result and upload_result.get("success"):
            return UploadResult(
                success=True,
                url=upload_result.get("url", ""),
                storage_id=upload_result.get("fileId", ""),
                metadata={
                    "folder": folder_name,
                    "name": remote_name,
                    "method": "box_rest_api",
                    "size": file_size,
                },
            )
        else:
            error = upload_result.get("error", "Unknown") if upload_result else "No response"
            try:
                page.screenshot(path=str(get_user_state_dir() / "box-debug.png"))
            except Exception:
                pass
            return UploadResult(
                success=False,
                url="",
                error=f"Box upload failed: {error}",
            )

    def clear_session(self) -> None:
        state_file = self.session_dir / "state.json"
        if state_file.exists():
            state_file.unlink()

    # StorageProvider interface
    def list_files(self, folder: str = "") -> list[StorageEntry]:
        return []

    def list_artifacts(self, folder: str = "") -> list[StorageEntry] | list[dict]:
        return []

    def download(self, storage_id: str, local_path: Path) -> bool:
        return False

    def delete(self, storage_id: str) -> bool:
        return False

    def move(self, source: str, destination: str) -> UploadResult | bool:
        return False

    def get_canonical_url(self, storage_id: str) -> str:
        if storage_id:
            return f"https://app.box.com/file/{storage_id}"
        return ""


# Register with factory
try:
    PublisherFactory.register("storage", "box-browser", BoxBrowserStorageProvider)
except Exception:
    pass
