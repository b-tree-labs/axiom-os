# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""OneDrive Browser Storage — uploads .docx files via Playwright.

No MS Graph API credentials needed. Uses the same browser session model
as the Teams browser extractor: first run opens a browser for login,
subsequent runs are fully headless.

Session cookies stored at ~/<state-dir>/credentials/onedrive/ (user-scoped).

Usage:
    # First time (opens browser for login)
    axi pub push --storage onedrive-browser --headed docs/my-doc.docx

    # Subsequent runs (headless)
    axi pub push --storage onedrive-browser docs/my-doc.docx

    # Bulk push all generated .docx files
    axi pub push --storage onedrive-browser --all

Requires: pip install playwright && playwright install chromium
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from axiom.infra.paths import get_user_state_dir

from ...factory import PublisherFactory
from ..base import StorageEntry, StorageProvider, UploadResult

logger = logging.getLogger(__name__)


def _get_session_dir() -> Path:
    return get_user_state_dir() / "credentials" / "onedrive"


# Default SharePoint/OneDrive folder for published docs
_DEFAULT_FOLDER = "/Documents/Axiom/"
_DEFAULT_DRAFT_FOLDER = "/Documents/Axiom/Drafts/"


class OneDriveBrowserStorageProvider(StorageProvider):
    """Microsoft OneDrive/SharePoint storage via Playwright browser automation.

    Authenticates as the user via their regular Microsoft account.
    No developer API credentials needed.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}
        self.session_dir = Path(
            config.get("session_dir")
            or os.environ.get("AXIOM_ONEDRIVE_SESSION_DIR")
            or str(_get_session_dir())
        )
        self.target_folder = config.get("folder", _DEFAULT_FOLDER)
        self.draft_folder = config.get("draft_folder", _DEFAULT_DRAFT_FOLDER)
        self.headless = config.get("headless", True)
        self.site_url = config.get(
            "site_url",
            os.environ.get("AXIOM_SHAREPOINT_URL", ""),
        )
        # Track current folder to avoid redundant navigation in batch uploads
        self._current_folder: str | None = None

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
        """Upload a file to OneDrive/SharePoint via browser.

        Args:
            local_path: Path to the local .docx file
            destination: Remote path/filename on OneDrive (defaults to local filename)
            metadata: Upload metadata (ignored for browser upload)
            draft: If True, upload to draft folder
            headed: If True, show browser (for first-time login)

        Returns:
            UploadResult with URL and metadata
        """
        self._ensure_playwright()
        from playwright.sync_api import sync_playwright

        if not local_path.exists():
            return UploadResult(
                success=False,
                url="",
                error=f"File not found: {local_path}",
            )

        remote_name = destination or local_path.name
        target = self.draft_folder if draft else self.target_folder
        headless = not headed and self.headless

        # Force headed for first login
        if not self.has_session() and headless:
            headless = False
            logger.info("No saved session — launching browser for login.")

        self.session_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(
                storage_state=str(self.session_dir / "state.json") if self.has_session() else None,
            )
            page = context.new_page()

            try:
                result = self._upload_to_onedrive(
                    page,
                    context,
                    local_path,
                    remote_name,
                    target,
                )

                # Save session
                context.storage_state(path=str(self.session_dir / "state.json"))
                os.chmod(str(self.session_dir / "state.json"), 0o600)

                return result

            except Exception as e:
                # Save session even on failure
                try:
                    context.storage_state(path=str(self.session_dir / "state.json"))
                    os.chmod(str(self.session_dir / "state.json"), 0o600)
                except Exception:
                    pass
                return UploadResult(
                    success=False,
                    url="",
                    error=str(e),
                )
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
        """Upload multiple files in a single browser session.

        More efficient than calling upload() per file — reuses the same
        browser context and authentication.

        Args:
            folders: Optional per-file folder paths. If provided, must be
                     same length as files. Otherwise uses default target folder.
        """
        self._ensure_playwright()
        from playwright.sync_api import sync_playwright

        headless = not headed and self.headless
        if not self.has_session() and headless:
            headless = False

        self.session_dir.mkdir(parents=True, exist_ok=True)
        default_target = self.draft_folder if draft else self.target_folder
        results: list[UploadResult] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(
                storage_state=str(self.session_dir / "state.json") if self.has_session() else None,
            )
            page = context.new_page()

            try:
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

                    target = folders[i] if folders else default_target
                    print(
                        f"    [{i + 1}/{len(files)}] Uploading {local_path.name}...",
                        end=" ",
                        flush=True,
                    )
                    result = self._upload_to_onedrive(
                        page,
                        context,
                        local_path,
                        local_path.name,
                        target,
                    )
                    results.append(result)
                    if result.success:
                        print("\u2713", flush=True)
                    else:
                        error_msg = result.error[:120]
                        if "locked" in error_msg.lower():
                            # File is open/locked — skip and continue with next file
                            print("\u26a0 locked (skipping — close the file and retry)", flush=True)
                        else:
                            print(f"\u2717 {error_msg}", flush=True)

                context.storage_state(path=str(self.session_dir / "state.json"))
                os.chmod(str(self.session_dir / "state.json"), 0o600)

            except Exception as e:
                try:
                    context.storage_state(path=str(self.session_dir / "state.json"))
                    os.chmod(str(self.session_dir / "state.json"), 0o600)
                except Exception:
                    pass
                # Mark remaining files as failed
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

    def _resolve_onedrive_url(self) -> str:
        """Resolve the correct OneDrive URL.

        If site_url is configured, use it directly.
        Otherwise, navigate to office.com/launch/onedrive — Microsoft's
        universal entry point that redirects to the user's org OneDrive
        after SSO. No URL guessing needed.
        """
        if self.site_url:
            return self.site_url

        # Check if we previously discovered the org URL
        # (must contain /my or /personal/ to be a usable files URL,
        # not just the home page)
        discovered = self._load_discovered_url()
        if discovered and ("/my" in discovered or "/personal/" in discovered):
            return discovered

        # Universal entry point — works for any org, personal, or edu account.
        # Microsoft handles the redirect to the correct OneDrive instance.
        # The actual URL is captured and saved after the first successful redirect.
        return "https://www.office.com/launch/onedrive"

    def _upload_to_onedrive(
        self,
        page,
        context,
        local_path: Path,
        remote_name: str,
        target_folder: str,
    ) -> UploadResult:
        """Upload a file to OneDrive using Graph API with browser session cookies.

        Instead of clicking UI elements (brittle), we:
        1. Navigate to OneDrive to establish session + get auth tokens
        2. Extract the access token from the browser's cookies/localStorage
        3. Use the Graph API directly for file upload (reliable, no CSS selectors)
        """

        # Skip full navigation if we're already in the target folder (batch optimization)
        already_in_folder = self._current_folder == target_folder

        if not already_in_folder:
            onedrive_url = self._resolve_onedrive_url()
            page.goto(onedrive_url, wait_until="domcontentloaded", timeout=30000)

            # Handle login if needed
            if self._needs_login(page):
                self._do_login(page)

            page.wait_for_timeout(3000)

        # Upload via OneDrive web UI (tested: UT SharePoint, March 2026)
        try:
            if not already_in_folder:
                # Navigate to "My files" — this resolves the personal site URL
                try:
                    page.click("text=My files", timeout=5000)
                    page.wait_for_timeout(3000)
                except Exception:
                    pass

                # Save the discovered URL AFTER navigating to My files
                actual_url = page.url
                if "sharepoint.com" in actual_url:
                    self._save_discovered_url(actual_url)

                # Navigate into target folder using DOUBLE-CLICK
                parts = [fp for fp in target_folder.strip("/").split("/") if fp]
                missing_from = None

                for i, part in enumerate(parts):
                    folder_el = page.query_selector(
                        f"[data-automationid='field-LinkFilename']:has-text('{part}')"
                    )
                    if folder_el:
                        folder_el.dblclick()
                        page.wait_for_timeout(3000)
                    else:
                        # This folder and all remaining don't exist
                        missing_from = i
                        break
            else:
                missing_from = None
                parts = [fp for fp in target_folder.strip("/").split("/") if fp]

            if missing_from is not None:
                # Use "Folder upload" to create missing folders + upload in one step.
                # Build a temp directory with the missing folder structure.
                import shutil
                import tempfile

                remaining_parts = parts[missing_from:]
                tmp_root = Path(tempfile.mkdtemp())
                nested = tmp_root
                for part in remaining_parts:
                    nested = nested / part
                nested.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(local_path), str(nested / local_path.name))

                # Upload the top-level missing folder
                upload_dir = tmp_root / remaining_parts[0]

                self._click_create_or_upload(page)
                page.wait_for_timeout(1000)
                with page.expect_file_chooser(timeout=10000) as fc_info:
                    page.click("text=Folder upload", timeout=5000)
                fc = fc_info.value
                fc.set_files(str(upload_dir))
                page.wait_for_timeout(8000)

                shutil.rmtree(tmp_root, ignore_errors=True)
            else:
                # All folders exist — upload file directly
                self._click_create_or_upload(page)
                page.wait_for_timeout(2000)  # Wait for dropdown flyout

                # Try multiple label variants for the upload menu item
                with page.expect_file_chooser(timeout=15000) as fc_info:
                    uploaded = False
                    for label in ["Files upload", "File upload", "Upload files", "Upload"]:
                        btn = page.query_selector(f"text={label}")
                        if btn:
                            btn.click()
                            uploaded = True
                            break
                    if not uploaded:
                        # Fallback: click any menu item containing "upload" (case-insensitive)
                        page.click("[role='menuitem'] >> text=/upload/i", timeout=5000)
                fc = fc_info.value
                fc.set_files(str(local_path))

                # Wait for Replace dialog OR upload completion.
                # OneDrive shows "Replace" when a file with the same name exists.
                # We MUST click Replace to preserve the file's item ID and URL.
                replaced = self._wait_for_replace_dialog(page)
                if replaced is None:
                    # No dialog appeared — file was new, upload completed
                    pass
                elif not replaced:
                    # Dialog appeared but we couldn't click Replace
                    return UploadResult(
                        success=False,
                        url="",
                        error="Replace dialog appeared but could not be confirmed",
                    )

                # Check for error toast (file locked, permission denied, etc.)
                error = self._check_upload_error(page)
                if error:
                    return UploadResult(success=False, url="", error=error)

                # Verify no duplicate was created (e.g., "filename (1).docx")
                duplicate = self._check_for_duplicate(page, remote_name)
                if duplicate:
                    return UploadResult(
                        success=False,
                        url="",
                        error=f"Duplicate created: '{duplicate}'. Replace dialog may have been missed. "
                        f"Delete the duplicate on OneDrive and retry.",
                    )

            self._current_folder = target_folder
            return UploadResult(
                success=True,
                url=page.url,
                storage_id=remote_name,
                metadata={
                    "folder": target_folder,
                    "name": remote_name,
                    "method": "onedrive_ui",
                },
            )

        except Exception as e:
            self._current_folder = None  # Reset on failure
            try:
                page.screenshot(path=str(get_user_state_dir() / "onedrive-debug.png"))
            except Exception:
                pass
            return UploadResult(success=False, url="", error=f"Upload failed: {e}")

    def _click_create_or_upload(self, page, timeout_ms: int = 8000) -> None:
        """Click the OneDrive 'Create or upload' / 'Upload' / 'Add new' button.

        OneDrive's toolbar button text has changed across UI refreshes. We try
        multiple known labels before falling back to a CSS/role-based selector.
        Raises on total failure so callers can surface a clean error.
        """
        candidates = [
            # Text labels — ordered most-recent-first
            "text=Upload",
            "text=Add new",
            "text=New",
            "text=Create or upload",
            # ARIA role fallbacks
            "[aria-label='Upload']",
            "[aria-label='Add new']",
            "[aria-label='New']",
            "[aria-label='Create or upload']",
            # Generic toolbar button containing 'upload' anywhere
            "button:has-text('Upload')",
            "button:has-text('Add new')",
        ]
        for selector in candidates:
            try:
                page.click(selector, timeout=timeout_ms // len(candidates))
                return
            except Exception:
                continue
        raise RuntimeError(
            "Could not find the OneDrive upload/add-new toolbar button. "
            "The OneDrive UI may have changed — run with --headed to inspect manually."
        )

    def _wait_for_replace_dialog(self, page, timeout_ms: int = 8000) -> bool | None:
        """Wait for the OneDrive 'Replace' confirmation dialog.

        Returns:
            True  — dialog appeared and Replace was clicked successfully
            False — dialog appeared but Replace click failed
            None  — no dialog appeared (file was new, no conflict)
        """
        # Poll for the Replace button with increasing waits.
        # OneDrive's dialog can take 1-5 seconds to appear depending on file size.
        elapsed = 0
        poll_interval = 500
        while elapsed < timeout_ms:
            # Try multiple selector strategies for the Replace button
            for selector in [
                "button:has-text('Replace')",
                "[data-automationid='confirmButton']:has-text('Replace')",
                "button.ms-Button--primary:has-text('Replace')",
                "button:has-text('replace')",  # case variation
            ]:
                btn = page.query_selector(selector)
                if btn:
                    try:
                        btn.click()
                        page.wait_for_timeout(3000)
                        logger.info("Clicked Replace — file updated in-place (URL preserved)")
                        return True
                    except Exception as e:
                        logger.warning("Replace button found but click failed: %s", e)
                        return False

            # Also check if upload already completed (progress bar gone, no dialog)
            # If we see the file in the list and no dialog, upload succeeded as new file
            progress = page.query_selector("[role='progressbar'], .od-FileUpload")
            if not progress and elapsed > 3000:
                # No progress bar and no dialog after 3s — upload completed without conflict
                return None

            page.wait_for_timeout(poll_interval)
            elapsed += poll_interval

        # Timeout — no dialog appeared
        return None

    def _check_upload_error(self, page) -> str | None:
        """Check for OneDrive error toasts after upload attempt.

        Returns error message string, or None if no error detected.
        """
        page.wait_for_timeout(1000)
        for selector in [
            "[class*='error']",
            "[class*='Error']",
            "[role='alert']",
            ".od-ErrorToast",
            "[data-automationid='errorMessage']",
        ]:
            toast = page.query_selector(selector)
            if toast:
                text = toast.inner_text().strip()
                if text and (
                    "locked" in text.lower()
                    or "error" in text.lower()
                    or "denied" in text.lower()
                    or "failed" in text.lower()
                ):
                    return f"OneDrive rejected upload: {text[:200]}"
        return None

    def _check_for_duplicate(self, page, original_name: str) -> str | None:
        """Check if a duplicate file was created instead of replacing.

        OneDrive creates 'filename (1).docx' when Replace is missed.
        Returns the duplicate filename if found, None otherwise.
        """
        stem = Path(original_name).stem
        suffix = Path(original_name).suffix

        # Look for common duplicate patterns: "name (1).docx", "name (2).docx"
        for i in range(1, 4):
            dup_name = f"{stem} ({i}){suffix}"
            dup_el = page.query_selector(
                f"[data-automationid='field-LinkFilename']:has-text('{dup_name}')"
            )
            if dup_el:
                logger.warning(
                    "Duplicate detected: %s (Replace dialog may have been missed)", dup_name
                )
                return dup_name

        return None

    def _extract_access_token(self, page, context) -> str | None:
        """Extract a Graph API access token from the browser session.

        OneDrive's web app stores tokens in sessionStorage/localStorage.
        We can also get tokens by intercepting API calls the page makes.
        """
        # Method 1: Check localStorage for cached tokens
        try:
            token = page.evaluate("""() => {
                // OneDrive stores tokens in various localStorage keys
                for (const key of Object.keys(localStorage)) {
                    const val = localStorage.getItem(key);
                    if (val && val.includes('eyJ') && val.includes('access_token')) {
                        try {
                            const parsed = JSON.parse(val);
                            if (parsed.access_token) return parsed.access_token;
                            if (parsed.secret) return parsed.secret;
                        } catch(e) {}
                    }
                }
                // Check sessionStorage
                for (const key of Object.keys(sessionStorage)) {
                    const val = sessionStorage.getItem(key);
                    if (val && val.includes('eyJ') && val.includes('access_token')) {
                        try {
                            const parsed = JSON.parse(val);
                            if (parsed.access_token) return parsed.access_token;
                            if (parsed.secret) return parsed.secret;
                        } catch(e) {}
                    }
                }
                return null;
            }""")
            if token:
                return token
        except Exception:
            pass

        # Method 2: Intercept a Graph API call that OneDrive makes
        try:
            # Navigate to trigger an API call, intercept the auth header
            access_token = None

            def handle_request(request):
                nonlocal access_token
                auth = request.headers.get("authorization", "")
                if auth.startswith("Bearer ") and "graph.microsoft.com" in request.url:
                    access_token = auth[7:]

            page.on("request", handle_request)
            # Trigger a navigation that causes OneDrive to make an API call
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            page.remove_listener("request", handle_request)

            if access_token:
                return access_token
        except Exception:
            pass

        return None

    def _needs_login(self, page) -> bool:
        url = page.url
        return (
            "login.microsoftonline.com" in url
            or "login.live.com" in url
            or "login.microsoft.com" in url
        )

    def _do_login(self, page) -> None:
        if self.headless:
            raise RuntimeError("Login required but running headless. Run with --headed first.")
        print("\n  Microsoft login required.")
        print("  Complete login + MFA in the browser window.\n")
        try:
            page.wait_for_url(
                re.compile(r"(onedrive|sharepoint)\."),
                timeout=300_000,
            )
            print("  ✓ Login successful.\n")
        except Exception:
            raise RuntimeError("Login timed out (5 minute limit).")

    def _navigate_to_folder(self, page, folder_path: str) -> None:
        """Navigate to a folder in OneDrive, creating it if needed."""
        parts = [p for p in folder_path.strip("/").split("/") if p]

        for part in parts:
            folder_link = page.query_selector(
                f"[data-automationid='FieldRenderer-name']:has-text('{part}')"
            )
            if folder_link:
                folder_link.click()
                page.wait_for_timeout(1500)
            else:
                # Create the folder
                new_btn = page.query_selector(
                    "button:has-text('New'), [data-automationid='newCommand']"
                )
                if new_btn:
                    new_btn.click()
                    page.wait_for_timeout(500)
                    folder_option = page.query_selector(
                        "button:has-text('Folder'), [data-automationid='newFolderCommand']"
                    )
                    if folder_option:
                        folder_option.click()
                        page.wait_for_timeout(500)
                        # Type folder name
                        name_input = page.query_selector(
                            "input[type='text'], [data-automationid='TextField']"
                        )
                        if name_input:
                            name_input.fill(part)
                            page.keyboard.press("Enter")
                            page.wait_for_timeout(1500)
                            # Navigate into the new folder
                            new_folder = page.query_selector(
                                f"[data-automationid='FieldRenderer-name']:has-text('{part}')"
                            )
                            if new_folder:
                                new_folder.click()
                                page.wait_for_timeout(1000)

    def _get_file_url(self, page, filename: str) -> str:
        """Try to get the sharing URL for an uploaded file."""
        file_link = page.query_selector(
            f"[data-automationid='FieldRenderer-name']:has-text('{filename}')"
        )
        if file_link:
            # Right-click → "Share" or "Copy link"
            file_link.click(button="right")
            page.wait_for_timeout(500)

            share_btn = page.query_selector(
                "button:has-text('Share'), [data-automationid='shareCommand']"
            )
            if share_btn:
                share_btn.click()
                page.wait_for_timeout(1000)

                copy_link = page.query_selector(
                    "button:has-text('Copy link'), [data-automationid='copyLinkCommand']"
                )
                if copy_link:
                    copy_link.click()
                    page.wait_for_timeout(500)
                    # Close share dialog
                    close = page.query_selector("button[aria-label='Close']")
                    if close:
                        close.click()
                    # The URL is now in clipboard — we can't access it directly
                    # Return the page URL as a fallback
                    return page.url

            # Close context menu
            page.keyboard.press("Escape")

        return page.url

    def _save_discovered_url(self, url: str) -> None:
        """Save the discovered org OneDrive URL for future headless runs."""
        try:
            discovered_file = self.session_dir / "discovered_url.txt"
            discovered_file.parent.mkdir(parents=True, exist_ok=True)
            discovered_file.write_text(url)
            logger.info("Discovered OneDrive URL saved: %s", url)
        except Exception:
            pass

    def _load_discovered_url(self) -> str:
        """Load a previously discovered OneDrive URL."""
        try:
            discovered_file = self.session_dir / "discovered_url.txt"
            if discovered_file.exists():
                return discovered_file.read_text().strip()
        except Exception:
            pass
        return ""

    def clear_session(self) -> None:
        """Clear saved browser session."""
        state_file = self.session_dir / "state.json"
        if state_file.exists():
            state_file.unlink()

    # StorageProvider interface methods

    def list_files(self, folder: str = "") -> list[StorageEntry]:
        return []  # Not implemented for browser provider

    def download(self, storage_id: str, local_path: Path) -> bool:
        return False  # Use OneDrive web UI directly

    def delete(self, storage_id: str) -> bool:
        return False  # Not implemented

    def get_canonical_url(self, storage_id: str) -> str:
        return ""  # URL returned during upload

    def list_artifacts(self, folder: str = "") -> list[StorageEntry] | list[dict]:
        return []  # Not implemented for browser provider

    def move(self, source: str, destination: str) -> UploadResult | bool:
        return False  # Not implemented for browser provider


# Register with the publisher factory
try:
    PublisherFactory.register(
        "storage",
        "onedrive-browser",
        OneDriveBrowserStorageProvider,
    )
except Exception:
    pass  # Factory may not be initialized yet
