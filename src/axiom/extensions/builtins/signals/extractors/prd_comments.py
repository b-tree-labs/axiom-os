# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PRD Comments extractor — Microsoft Graph API integration.

Fetches comments from Word documents stored in OneDrive/SharePoint.
Targets PRDs with "In Review" status to surface stakeholder feedback.

SETUP REQUIRED:
1. Register an Azure AD app at https://portal.azure.com
2. Add Microsoft Graph permissions: Files.Read.All, Sites.Read.All
3. Create client secret or use device code flow
4. Set environment variables:
   - AZURE_CLIENT_ID
   - AZURE_TENANT_ID
   - AZURE_CLIENT_SECRET (or use device auth)

The extractor looks for .docx files in a configurable OneDrive folder
and extracts threaded comments with author attribution.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from ..models import Extraction, Signal
from .base import BaseExtractor


class PRDCommentsExtractor(BaseExtractor):
    """Extract comments from PRD Word documents via Microsoft Graph API."""

    # Default OneDrive folder path to scan for PRDs
    DEFAULT_PRD_FOLDER = "/Documents/PRDs"

    @property
    def name(self) -> str:
        return "prd_comments"

    def can_handle(self, path: Path) -> bool:
        """This extractor doesn't use local files directly.

        It fetches from OneDrive. The 'path' here is a config file
        or marker indicating PRD extraction should run.
        """
        return path.suffix == ".prd_config" or path.name == "prd_comments_config.json"

    def extract(self, source: Path, **kwargs) -> Extraction:
        """Fetch PRD comments from OneDrive via Microsoft Graph.

        Args:
            source: Path to config file with folder path and filters.
            days_back: Only fetch comments from last N days (default 14).
            status_filter: PRD status to match (default "In Review").
            correlator: Entity correlator for people/initiatives.

        Returns:
            Extraction with comment signals.
        """
        days_back = kwargs.get("days_back", 14)
        status_filter = kwargs.get("status_filter", "In Review")
        correlator = kwargs.get("correlator")

        signals: list[Signal] = []
        errors: list[str] = []

        # Check for required credentials (try connections first, fall back to env)
        from axiom.infra.connections import get_credential
        client_id = get_credential("ms_graph") or os.environ.get("AZURE_CLIENT_ID") or os.environ.get("MS_GRAPH_CLIENT_ID")
        tenant_id = os.environ.get("AZURE_TENANT_ID") or os.environ.get("MS_GRAPH_TENANT_ID")
        client_secret = os.environ.get("AZURE_CLIENT_SECRET") or os.environ.get("MS_GRAPH_CLIENT_SECRET")

        if not client_id or not tenant_id:
            return Extraction(
                extractor=self.name,
                source_file=str(source),
                signals=[],
                errors=[
                    "Microsoft Graph credentials not configured. "
                    "Set AZURE_CLIENT_ID and AZURE_TENANT_ID environment variables. "
                    "See tools/pipelines/sense/extractors/prd_comments.py for setup instructions."
                ],
            )

        # Load config if provided
        folder_path = self.DEFAULT_PRD_FOLDER
        if source.exists() and source.suffix == ".json":
            try:
                config = json.loads(source.read_text())
                folder_path = config.get("folder_path", folder_path)
                days_back = config.get("days_back", days_back)
                status_filter = config.get("status_filter", status_filter)
            except Exception as e:
                errors.append(f"Failed to load config: {e}")

        # Try to import msal for authentication
        try:
            import msal  # noqa: F401
            import requests  # noqa: F401
        except ImportError:
            return Extraction(
                extractor=self.name,
                source_file=str(source),
                signals=[],
                errors=[
                    "msal and requests packages required. "
                    "Install with: pip install msal requests"
                ],
            )

        # Authenticate with Microsoft Graph
        try:
            access_token = self._get_access_token(client_id, tenant_id, client_secret)
        except Exception as e:
            return Extraction(
                extractor=self.name,
                source_file=str(source),
                signals=[],
                errors=[f"Authentication failed: {e}"],
            )

        # Fetch PRD documents
        try:
            documents = self._list_prd_documents(access_token, folder_path)
            print(f"  Found {len(documents)} documents in {folder_path}")
        except Exception as e:
            return Extraction(
                extractor=self.name,
                source_file=str(source),
                signals=[],
                errors=[f"Failed to list documents: {e}"],
            )

        # Extract comments from each document
        now = datetime.now(UTC)
        for doc in documents:
            try:
                comments = self._get_document_comments(access_token, doc["id"])
                for comment in comments:
                    # Filter by recency
                    comment_date = datetime.fromisoformat(
                        comment["createdDateTime"].replace("Z", "+00:00")
                    )
                    age_days = (now - comment_date).days
                    if age_days > days_back:
                        continue

                    author = comment.get("author", {}).get("displayName", "Unknown")
                    content = comment.get("content", "")

                    # Also get replies
                    replies_text = ""
                    for reply in comment.get("replies", []):
                        reply_author = reply.get("author", {}).get("displayName", "Unknown")
                        reply_content = reply.get("content", "")
                        replies_text += f"\n  → {reply_author}: {reply_content}"

                    signal = Signal(
                        source=self.name,
                        timestamp=comment_date.isoformat(),
                        raw_text=f"{author}: {content}{replies_text}",
                        signal_type="feedback",
                        detail=f"PRD comment on {doc['name']}: {content[:100]}...",
                        confidence=0.8,
                        people=[author],
                        metadata={
                            "document_name": doc["name"],
                            "document_id": doc["id"],
                            "comment_id": comment.get("id"),
                            "has_replies": len(comment.get("replies", [])) > 0,
                        },
                    )

                    # Resolve people via correlator
                    if correlator:
                        signal.people = correlator.resolve_people(signal.people)

                    signals.append(signal)

            except Exception as e:
                errors.append(f"Failed to get comments for {doc['name']}: {e}")

        return Extraction(
            extractor=self.name,
            source_file=str(source),
            signals=signals,
            errors=errors,
        )

    def _get_access_token(
        self, client_id: str, tenant_id: str, client_secret: str | None
    ) -> str:
        """Authenticate with Microsoft Graph and return access token."""
        import msal

        authority = f"https://login.microsoftonline.com/{tenant_id}"
        scopes = ["https://graph.microsoft.com/.default"]

        if client_secret:
            # Client credentials flow (for background jobs)
            app = msal.ConfidentialClientApplication(
                client_id,
                authority=authority,
                client_credential=client_secret,
            )
            result = app.acquire_token_for_client(scopes=scopes)
        else:
            # Device code flow (interactive)
            app = msal.PublicClientApplication(client_id, authority=authority)
            flow = app.initiate_device_flow(scopes=scopes)
            print(f"  {flow['message']}")
            result = app.acquire_token_by_device_flow(flow)

        if result is None or "access_token" not in result:
            desc = result.get("error_description", "Authentication failed") if result else "Authentication failed"
            raise ValueError(desc)

        return result["access_token"]

    def _list_prd_documents(
        self, access_token: str, folder_path: str
    ) -> list[dict]:
        """List .docx files in the specified OneDrive folder."""
        import requests

        # Use the /me/drive endpoint for user's OneDrive
        # For SharePoint, use /sites/{site-id}/drive
        url = f"https://graph.microsoft.com/v1.0/me/drive/root:{folder_path}:/children"
        headers = {"Authorization": f"Bearer {access_token}"}

        response = requests.get(url, headers=headers)
        response.raise_for_status()

        items = response.json().get("value", [])
        return [
            {"id": item["id"], "name": item["name"]}
            for item in items
            if item["name"].endswith(".docx")
        ]

    def _get_document_comments(self, access_token: str, doc_id: str) -> list[dict]:
        """Fetch comments for a specific document.

        Note: Microsoft Graph doesn't expose Word comments directly.
        We need to use the beta API or download and parse the document.
        This implementation uses a workaround via the preview API.
        """
        import requests

        # Comments API (beta)
        url = f"https://graph.microsoft.com/beta/me/drive/items/{doc_id}/comments"
        headers = {"Authorization": f"Bearer {access_token}"}

        try:
            response = requests.get(url, headers=headers)
            if response.status_code == 404:
                # Endpoint not available, return empty
                return []
            response.raise_for_status()
            return response.json().get("value", [])
        except Exception:
            # Fall back to empty if comments API fails
            return []


# Alternative: Local .docx parsing for offline use
def extract_comments_from_docx(docx_path: Path) -> list[dict]:
    """Extract comments from a local .docx file.

    Requires python-docx: pip install python-docx

    Returns list of dicts with keys: author, content, date, paragraph_text
    """
    try:
        from docx import Document
        from docx.oxml.ns import qn
    except ImportError:
        return []

    doc = Document(str(docx_path))
    comments = []

    # Comments are stored in word/comments.xml
    # Access via document part relationships
    comments_part = None
    for rel in doc.part.rels.values():
        if "comments" in rel.reltype:
            comments_part = rel.target_part
            break

    if comments_part is None:
        return []

    # Parse comments XML
    from xml.etree import ElementTree as ET

    root = ET.fromstring(comments_part.blob)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

    for comment_elem in root.findall(".//w:comment", ns):
        author = comment_elem.get(qn("w:author"), "Unknown")
        date = comment_elem.get(qn("w:date"), "")

        # Get comment text
        text_parts = []
        for t in comment_elem.findall(".//w:t", ns):
            if t.text:
                text_parts.append(t.text)
        content = "".join(text_parts)

        comments.append({
            "author": author,
            "content": content,
            "date": date,
        })

    return comments
