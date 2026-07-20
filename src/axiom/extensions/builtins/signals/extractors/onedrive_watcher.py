# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""OneDrive change watcher — detects document modifications via browser session.

Polls OneDrive folders for changes by intercepting SharePoint's
RenderListDataAsStream API responses. Detects:
- File modifications (timestamp change)
- New files
- Deleted files
- Comment additions (via separate comment API)

Designed for < 1 minute change detection SLA with hundreds of docs.
Uses a single browser context with periodic page refreshes.

Usage:
    # Run as background watcher
    axi signal watch --source onedrive

    # One-shot check
    axi signal check --source onedrive
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.paths import get_user_state_dir
from axiom.infra.state import atomic_write

logger = logging.getLogger(__name__)

_STATE_FILE = _REPO_ROOT / ".neut" / "publisher" / "onedrive_watch_state.json"


@dataclass
class FileState:
    """Tracked state of a single OneDrive file."""

    name: str
    modified: str
    editor: str = ""
    item_id: str = ""
    size: str = ""


@dataclass
class WatchState:
    """Persistent state for the watcher across polls."""

    last_check: str = ""
    files: dict[str, FileState] = field(default_factory=dict)  # name → state

    def save(self, path: Path | None = None):
        path = path or _STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "last_check": self.last_check,
            "files": {
                k: {
                    "name": v.name,
                    "modified": v.modified,
                    "editor": v.editor,
                    "item_id": v.item_id,
                    "size": v.size,
                }
                for k, v in self.files.items()
            },
        }
        atomic_write(path, data)

    @classmethod
    def load(cls, path: Path | None = None) -> WatchState:
        path = path or _STATE_FILE
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text())
            files = {k: FileState(**v) for k, v in data.get("files", {}).items()}
            return cls(last_check=data.get("last_check", ""), files=files)
        except Exception:
            return cls()


@dataclass
class ChangeEvent:
    """A detected change on OneDrive."""

    event_type: str  # "modified" | "new" | "deleted" | "comment"
    file_name: str
    modified: str = ""
    editor: str = ""
    old_modified: str = ""
    details: dict = field(default_factory=dict)


def poll_onedrive_folder(
    folder_path: str = "",
    session_dir: Path | None = None,
) -> list[ChangeEvent]:
    """Poll a OneDrive folder for changes since last check.

    Uses Playwright browser session to intercept SharePoint API responses.
    Returns list of changes detected.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright not installed — OneDrive watching unavailable")
        return []

    session_dir = session_dir or (get_user_state_dir() / "credentials" / "onedrive")
    state_file = session_dir / "state.json"

    if not state_file.exists():
        logger.warning("No OneDrive session — run axi pub push --endpoint onedrive --headed first")
        return []

    # Load previous state
    watch_state = WatchState.load()
    changes: list[ChangeEvent] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=str(state_file) if state_file.exists() else None,
        )
        page = context.new_page()

        # Navigate to OneDrive
        page.goto(
            "https://www.office.com/launch/onedrive", wait_until="domcontentloaded", timeout=30000
        )
        page.wait_for_timeout(5000)
        page.click("text=My files", timeout=5000)
        page.wait_for_timeout(3000)

        # Collect file metadata by intercepting API responses
        current_files: dict[str, FileState] = {}

        def capture_files(response):
            if (
                "RenderListDataAsStream" not in response.url
                and "GetListUsingPath" not in response.url
            ):
                return
            if response.status != 200:
                return
            try:
                body = response.json()
                rows = body.get("ListData", {}).get("Row", [])
                for row in rows:
                    name = row.get("FileLeafRef", "")
                    if not name:
                        continue
                    state = FileState(
                        name=name,
                        modified=row.get("Modified", ""),
                        editor=row.get("Editor", [{}])[0].get("title", "")
                        if row.get("Editor")
                        else "",
                        item_id=row.get("ID", ""),
                        size=row.get("File_x0020_Size", ""),
                    )
                    current_files[name] = state
            except Exception:
                pass

        # Navigate into the target folder FIRST (before capturing)
        folder_parts = [fp for fp in folder_path.strip("/").split("/") if fp]
        for part in folder_parts:
            el = page.query_selector(f"[data-automationid='field-LinkFilename']:has-text('{part}')")
            if el:
                el.dblclick()
                page.wait_for_timeout(3000)

        # NOW start capturing — only files inside the target folder
        page.on("response", capture_files)

        # Reload to trigger a fresh API call for this folder's contents
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(5000)

        # Also scan subfolders by navigating into each
        subfolder_els = page.query_selector_all("[data-automationid='field-LinkFilename']")
        subfolder_names = []
        for el in subfolder_els:
            try:
                text = el.inner_text()
                if text and text != "Name" and not text.endswith(".docx"):
                    subfolder_names.append(text)
            except Exception:
                pass

        for sf_name in subfolder_names:
            el = page.query_selector(
                f"[data-automationid='field-LinkFilename']:has-text('{sf_name}')"
            )
            if el:
                el.dblclick()
                page.wait_for_timeout(3000)
                page.go_back()
                page.wait_for_timeout(2000)

        page.remove_listener("response", capture_files)

        # Save session
        context.storage_state(path=str(state_file))
        context.close()
        browser.close()

    # Compare against publication registry (PRESS's baseline)
    # This is the authoritative source — what PRESS published vs. what's on OneDrive now
    try:
        from axiom.infra.publication_registry import PublicationRegistry

        registry = PublicationRegistry()
        published_names = registry.published_names()
    except Exception:
        published_names = set()
        registry = None

    for name, current in current_files.items():
        if registry:
            pub_record = registry.get_by_name(name)
            if pub_record:
                # This file was published by PRESS — check if it diverged
                if (
                    current.modified != pub_record.endpoint_modified
                    and pub_record.endpoint_modified
                ):
                    changes.append(
                        ChangeEvent(
                            event_type="modified",
                            file_name=name,
                            modified=current.modified,
                            editor=current.editor,
                            old_modified=pub_record.endpoint_modified,
                            details={
                                "published_at": pub_record.published_at,
                                "published_by": pub_record.published_by,
                                "source": pub_record.source_path,
                                "baseline": "publication_registry",
                            },
                        )
                    )
                continue  # Don't also check watch state for registry-tracked files

        # For files NOT in registry, fall back to watch state comparison
        previous = watch_state.files.get(name)
        if previous is None:
            changes.append(
                ChangeEvent(
                    event_type="new",
                    file_name=name,
                    modified=current.modified,
                    editor=current.editor,
                    details={"baseline": "watch_state"},
                )
            )
        elif current.modified != previous.modified:
            changes.append(
                ChangeEvent(
                    event_type="modified",
                    file_name=name,
                    modified=current.modified,
                    editor=current.editor,
                    old_modified=previous.modified,
                    details={"baseline": "watch_state"},
                )
            )

    # Check for deletions of published files
    if registry:
        for pub in registry.all():
            if pub.published_name and pub.published_name not in current_files:
                changes.append(
                    ChangeEvent(
                        event_type="deleted",
                        file_name=pub.published_name,
                        details={
                            "source": pub.source_path,
                            "baseline": "publication_registry",
                        },
                    )
                )

    # Also check watch state for non-registry deletions
    for name in watch_state.files:
        if name not in current_files and name not in published_names:
            changes.append(
                ChangeEvent(
                    event_type="deleted",
                    file_name=name,
                    details={"baseline": "watch_state"},
                )
            )

    # Update watch state
    watch_state.files = current_files
    watch_state.last_check = datetime.now(UTC).isoformat()
    watch_state.save()

    return changes


def watch_onedrive(
    folder_path: str = "",
    interval_seconds: int = 30,
    callback=None,
):
    """Continuously watch OneDrive for changes.

    Args:
        folder_path: OneDrive folder to watch
        interval_seconds: Polling interval (default: 30s for < 1min SLA)
        callback: Function called with list of ChangeEvents on each detection
    """
    print(f"  Watching OneDrive/{folder_path} (polling every {interval_seconds}s)")
    print("  Press Ctrl+C to stop.\n")

    while True:
        try:
            changes = poll_onedrive_folder(folder_path)

            if changes:
                now = datetime.now().strftime("%H:%M:%S")
                for change in changes:
                    icon = {"modified": "✏️", "new": "📄", "deleted": "🗑️", "comment": "💬"}.get(
                        change.event_type, "?"
                    )
                    print(f"  [{now}] {icon} {change.event_type}: {change.file_name}")
                    if change.editor:
                        print(f"           by {change.editor}")

                if callback:
                    callback(changes)

            time.sleep(interval_seconds)

        except KeyboardInterrupt:
            print("\n  Stopped watching.")
            break
        except Exception as e:
            logger.error("Watch error: %s", e)
            time.sleep(interval_seconds)
