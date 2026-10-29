# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Teams Chat Extractor — extracts signals from Microsoft Teams chat messages.

Supports:
- Channel messages
- Chat messages (1:1 and group)
- Message reactions (for priority signals)
- @mentions

Uses Microsoft Graph API with delegated permissions.

Required scopes:
- ChannelMessage.Read.All (for channel messages)
- Chat.Read (for chat messages)
- User.Read (for user info)

Usage:
    from axiom.extensions.builtins.signals.extractors.teams_chat import TeamsChatExtractor

    extractor = TeamsChatExtractor()

    # Fetch recent messages
    messages = extractor.fetch_messages(team_id="...", channel_id="...", days=7)

    # Or extract from exported JSON
    extraction = extractor.extract(export_json_path)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from axiom import REPO_ROOT as _REPO_ROOT

from ..models import Extraction, Signal
from ..registry import SourceType, register_source
from .base import BaseExtractor

_RUNTIME_DIR = _REPO_ROOT / "runtime"
TOKEN_PATH = _RUNTIME_DIR / "inbox" / "state" / "teams_chat_token.json"


@dataclass
class TeamsChatMessage:
    """A message from Teams chat or channel."""

    id: str
    content: str  # HTML content
    content_text: str  # Plain text
    sender: str
    sender_email: str
    timestamp: str
    channel_name: str = ""
    team_name: str = ""
    chat_type: str = ""  # "channel", "chat", "group"
    mentions: list[str] = field(default_factory=list)
    reactions: list[str] = field(default_factory=list)
    importance: str = "normal"
    has_attachments: bool = False
    reply_to_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "content_text": self.content_text,
            "sender": self.sender,
            "sender_email": self.sender_email,
            "timestamp": self.timestamp,
            "channel_name": self.channel_name,
            "team_name": self.team_name,
            "chat_type": self.chat_type,
            "mentions": self.mentions,
            "reactions": self.reactions,
            "importance": self.importance,
            "has_attachments": self.has_attachments,
            "reply_to_id": self.reply_to_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TeamsChatMessage:
        return cls(
            id=data["id"],
            content=data.get("content", ""),
            content_text=data.get("content_text", ""),
            sender=data.get("sender", ""),
            sender_email=data.get("sender_email", ""),
            timestamp=data.get("timestamp", ""),
            channel_name=data.get("channel_name", ""),
            team_name=data.get("team_name", ""),
            chat_type=data.get("chat_type", ""),
            mentions=data.get("mentions", []),
            reactions=data.get("reactions", []),
            importance=data.get("importance", "normal"),
            has_attachments=data.get("has_attachments", False),
            reply_to_id=data.get("reply_to_id"),
        )


@dataclass
class TeamsChatExport:
    """Exported Teams chat activity."""

    exported_at: str
    time_window_days: int
    messages: list[TeamsChatMessage] = field(default_factory=list)
    channels: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "exported_at": self.exported_at,
            "time_window_days": self.time_window_days,
            "messages": [m.to_dict() for m in self.messages],
            "channels": self.channels,
            "errors": self.errors,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TeamsChatExport:
        return cls(
            exported_at=data.get("exported_at", ""),
            time_window_days=data.get("time_window_days", 7),
            messages=[TeamsChatMessage.from_dict(m) for m in data.get("messages", [])],
            channels=data.get("channels", []),
            errors=data.get("errors", []),
        )


@register_source(
    name="teams_chat",
    description="Microsoft Teams chat messages and channels",
    source_type=SourceType.PULL,
    requires_auth=True,
    auth_env_vars=["MS_GRAPH_CLIENT_ID", "MS_GRAPH_TENANT_ID"],
    config_file="teams_chat_token.json",
    file_patterns=["*.json"],
    default_poll_interval=900,  # 15 minutes
    supports_webhook=True,
    icon="💬",
    category="communication",
)
class TeamsChatExtractor(BaseExtractor):
    """Extract signals from Teams chat messages via MS Graph API."""

    GRAPH_ENDPOINT = "https://graph.microsoft.com/v1.0"

    @property
    def name(self) -> str:
        return "teams_chat"

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        tenant_id: str | None = None,
        token_path: Path | None = None,
    ):
        from axiom.infra.connections import get_credential
        self.client_id = client_id or get_credential("ms_graph") or os.environ.get("MS_GRAPH_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("MS_GRAPH_CLIENT_SECRET")
        self.tenant_id = tenant_id or os.environ.get("MS_GRAPH_TENANT_ID", "common")
        self.token_path = token_path or TOKEN_PATH

        self._access_token: str | None = None
        self._token_expiry: datetime | None = None

    def is_available(self) -> bool:
        """Check if Teams access is configured."""
        return bool(self.client_id and self.client_secret)

    def can_handle(self, source_path: Path) -> bool:  # type: ignore[override]
        """Handle teams_chat export JSON files."""
        name = source_path.name.lower()
        return name.startswith("teams_chat") and name.endswith(".json")

    def _get_access_token(self) -> str:
        """Get a valid access token, refreshing if needed."""
        # Check cached token
        if self._access_token and self._token_expiry:
            if datetime.now(UTC) < self._token_expiry:
                return self._access_token

        # Try to load from file
        if self.token_path.exists():
            try:
                token_data = json.loads(self.token_path.read_text())
                expiry = datetime.fromisoformat(token_data["expires_at"])
                if datetime.now(UTC) < expiry:
                    self._access_token = token_data["access_token"]
                    self._token_expiry = expiry
                    assert self._access_token is not None
                    return self._access_token
            except (json.JSONDecodeError, KeyError):
                pass

        # Need to authenticate
        return self._authenticate()

    def _authenticate(self) -> str:
        """Authenticate via device code flow for delegated permissions."""
        if not self.client_id:
            raise RuntimeError("MS_GRAPH_CLIENT_ID not configured")

        # Start device code flow
        device_url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/devicecode"
        data = urllib.parse.urlencode({
            "client_id": self.client_id,
            "scope": "ChannelMessage.Read.All Chat.Read User.Read offline_access",
        }).encode()

        req = urllib.request.Request(device_url, data=data, method="POST")

        with urllib.request.urlopen(req, timeout=30) as resp:
            device_response = json.loads(resp.read().decode())

        print("\n=== Teams Chat Authentication ===")
        print(f"Go to: {device_response['verification_uri']}")
        print(f"Enter code: {device_response['user_code']}")
        print("Waiting for authentication...")

        # Poll for token
        token_url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
        interval = device_response.get("interval", 5)
        expires_in = device_response.get("expires_in", 900)
        deadline = datetime.now() + timedelta(seconds=expires_in)

        import time
        while datetime.now() < deadline:
            time.sleep(interval)

            poll_data = urllib.parse.urlencode({
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": self.client_id,
                "device_code": device_response["device_code"],
            }).encode()

            poll_req = urllib.request.Request(token_url, data=poll_data, method="POST")

            try:
                with urllib.request.urlopen(poll_req, timeout=30) as resp:
                    token_response = json.loads(resp.read().decode())

                    self._access_token = token_response["access_token"]
                    expires_in = token_response.get("expires_in", 3600)
                    self._token_expiry = datetime.now(UTC) + timedelta(seconds=expires_in - 60)

                    # Save token
                    token_data = {
                        "access_token": self._access_token,
                        "refresh_token": token_response.get("refresh_token"),
                        "expires_at": self._token_expiry.isoformat(),
                    }
                    self.token_path.parent.mkdir(parents=True, exist_ok=True)
                    self.token_path.write_text(json.dumps(token_data, indent=2))

                    print("  ✓ Teams authentication successful\n")
                    assert self._access_token is not None
                    return self._access_token

            except urllib.error.HTTPError as e:
                error_body = json.loads(e.read().decode())
                error_code = error_body.get("error")

                if error_code == "authorization_pending":
                    continue
                if error_code == "slow_down":
                    interval += 5
                    continue
                raise RuntimeError(f"Auth failed: {error_body.get('error_description', error_code)}")

        raise RuntimeError("Device code expired. Please try again.")

    def _graph_request(self, endpoint: str) -> dict:
        """Make an authenticated request to Graph API."""
        token = self._get_access_token()

        url = f"{self.GRAPH_ENDPOINT}{endpoint}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def fetch_joined_teams(self) -> list[dict]:
        """Get list of teams the user has joined."""
        result = self._graph_request("/me/joinedTeams")
        return result.get("value", [])

    def fetch_team_channels(self, team_id: str) -> list[dict]:
        """Get channels in a team."""
        result = self._graph_request(f"/teams/{team_id}/channels")
        return result.get("value", [])

    def fetch_channel_messages(
        self,
        team_id: str,
        channel_id: str,
        days: int = 7,
    ) -> list[TeamsChatMessage]:
        """Fetch recent messages from a channel."""
        # Note: This requires ChannelMessage.Read.All
        endpoint = f"/teams/{team_id}/channels/{channel_id}/messages"
        result = self._graph_request(endpoint)

        messages = []
        since = datetime.now(UTC) - timedelta(days=days)

        for msg in result.get("value", []):
            created = msg.get("createdDateTime", "")
            if created:
                msg_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if msg_time < since:
                    continue

            # Parse mentions
            mentions = []
            for mention in msg.get("mentions", []):
                mentioned = mention.get("mentioned", {})
                if user := mentioned.get("user"):
                    mentions.append(user.get("displayName", ""))

            # Get sender info
            sender_info = msg.get("from", {})
            user_info = sender_info.get("user", {})

            # Strip HTML from content
            content = msg.get("body", {}).get("content", "")
            content_text = self._strip_html(content)

            messages.append(TeamsChatMessage(
                id=msg.get("id", ""),
                content=content,
                content_text=content_text,
                sender=user_info.get("displayName", "Unknown"),
                sender_email=user_info.get("email", ""),
                timestamp=created,
                chat_type="channel",
                mentions=mentions,
                reactions=[r.get("reactionType", "") for r in msg.get("reactions", [])],
                importance=msg.get("importance", "normal"),
                has_attachments=bool(msg.get("attachments")),
                reply_to_id=msg.get("replyToId"),
            ))

        return messages

    def fetch_chats(self, days: int = 7) -> list[TeamsChatMessage]:
        """Fetch recent chat messages (1:1 and group)."""
        result = self._graph_request("/me/chats?$expand=members")
        chats = result.get("value", [])

        messages = []
        since = datetime.now(UTC) - timedelta(days=days)

        for chat in chats:
            chat_id = chat.get("id")
            chat_type = chat.get("chatType", "oneOnOne")

            # Get messages from this chat
            try:
                msg_result = self._graph_request(f"/me/chats/{chat_id}/messages")

                for msg in msg_result.get("value", []):
                    created = msg.get("createdDateTime", "")
                    if created:
                        msg_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        if msg_time < since:
                            continue

                    sender_info = msg.get("from", {})
                    user_info = sender_info.get("user", {})

                    content = msg.get("body", {}).get("content", "")
                    content_text = self._strip_html(content)

                    messages.append(TeamsChatMessage(
                        id=msg.get("id", ""),
                        content=content,
                        content_text=content_text,
                        sender=user_info.get("displayName", "Unknown"),
                        sender_email=user_info.get("email", ""),
                        timestamp=created,
                        chat_type=chat_type,
                        importance=msg.get("importance", "normal"),
                        has_attachments=bool(msg.get("attachments")),
                    ))
            except Exception as e:
                print(f"Warning: Could not fetch messages from chat {chat_id}: {e}")

        return messages

    def fetch_all_messages(
        self,
        days: int = 7,
        output_path: Path | None = None,
    ) -> TeamsChatExport:
        """Fetch all accessible Teams messages.

        Args:
            days: How many days back to fetch
            output_path: Optional path to save export JSON

        Returns:
            TeamsChatExport with all messages
        """
        export = TeamsChatExport(
            exported_at=datetime.now(UTC).isoformat(),
            time_window_days=days,
        )

        # Fetch from joined teams/channels
        try:
            teams = self.fetch_joined_teams()
            for team in teams:
                team_name = team.get("displayName", "")
                team_id = team.get("id")
                if not team_id:
                    continue

                channels = self.fetch_team_channels(team_id)
                for channel in channels:
                    channel_name = channel.get("displayName", "")
                    channel_id = channel.get("id")
                    if not channel_id:
                        continue

                    export.channels.append({
                        "team_name": team_name,
                        "team_id": team_id,
                        "channel_name": channel_name,
                        "channel_id": channel_id,
                    })

                    try:
                        messages = self.fetch_channel_messages(team_id, channel_id, days)
                        for msg in messages:
                            msg.team_name = team_name
                            msg.channel_name = channel_name
                        export.messages.extend(messages)
                    except Exception as e:
                        export.errors.append(f"Channel {team_name}/{channel_name}: {e}")
        except Exception as e:
            export.errors.append(f"Teams fetch error: {e}")

        # Fetch from chats
        try:
            chat_messages = self.fetch_chats(days)
            export.messages.extend(chat_messages)
        except Exception as e:
            export.errors.append(f"Chats fetch error: {e}")

        # Sort by timestamp
        export.messages.sort(key=lambda m: m.timestamp, reverse=True)

        # Save if output path specified
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(export.to_dict(), indent=2))

        return export

    def _strip_html(self, html: str) -> str:
        """Strip HTML tags from content."""
        import re
        text = re.sub(r'<[^>]+>', '', html)
        text = text.replace('&nbsp;', ' ')
        text = text.replace('&amp;', '&')
        text = text.replace('&lt;', '<')
        text = text.replace('&gt;', '>')
        return text.strip()

    def extract(self, source_path: Path, **kwargs) -> Extraction:  # type: ignore[override]
        """Extract signals from a Teams chat export JSON."""
        if not source_path.exists():
            return Extraction(
                extractor="teams_chat",
                source_file=str(source_path),
                errors=[f"File not found: {source_path}"],
            )

        try:
            data = json.loads(source_path.read_text())
            export = TeamsChatExport.from_dict(data)
        except Exception as e:
            return Extraction(
                extractor="teams_chat",
                source_file=str(source_path),
                errors=[f"Failed to parse: {e}"],
            )

        signals = []

        for msg in export.messages:
            # Skip system messages
            if not msg.content_text or msg.sender == "Unknown":
                continue

            # Determine signal type based on content
            signal_type = self._classify_message(msg)

            # Higher confidence for @mentions and high importance
            confidence = 0.7
            if msg.mentions:
                confidence = 0.85
            if msg.importance in ("high", "urgent"):
                confidence = 0.9

            # Build detail
            if msg.team_name and msg.channel_name:
                location = f"[{msg.team_name}/{msg.channel_name}]"
            elif msg.chat_type:
                location = f"[{msg.chat_type} chat]"
            else:
                location = ""

            sig = Signal(
                source="teams_chat",
                timestamp=msg.timestamp,
                raw_text=msg.content_text,
                people=[msg.sender] + msg.mentions if msg.sender else msg.mentions,
                signal_type=signal_type,
                detail=f"{location} {msg.sender}: {msg.content_text[:200]}",
                confidence=confidence,
                metadata={
                    "message_id": msg.id,
                    "team_name": msg.team_name,
                    "channel_name": msg.channel_name,
                    "chat_type": msg.chat_type,
                    "mentions": msg.mentions,
                    "reactions": msg.reactions,
                    "importance": msg.importance,
                },
            )
            signals.append(sig)

        return Extraction(
            extractor="teams_chat",
            source_file=str(source_path),
            signals=signals,
            errors=export.errors,
        )

    def _classify_message(self, msg: TeamsChatMessage) -> str:
        """Classify message into signal type."""
        text = msg.content_text.lower()

        # Check for blockers
        if any(kw in text for kw in ["blocked", "blocker", "stuck", "can't", "cannot", "waiting on"]):
            return "blocker"

        # Check for action items
        if any(kw in text for kw in ["todo", "to do", "action item", "please", "could you", "can you", "need to"]):
            return "action_item"

        # Check for decisions
        if any(kw in text for kw in ["decided", "decision", "approved", "agreed", "let's go with"]):
            return "decision"

        # Check for progress
        if any(kw in text for kw in ["done", "finished", "completed", "shipped", "merged", "deployed"]):
            return "progress"

        # Check for status updates
        if any(kw in text for kw in ["update", "status", "progress", "working on", "currently"]):
            return "status_change"

        return "raw"


# Convenience function for CLI
def export_teams_chat(
    days: int = 7,
    output_dir: Path | None = None,
) -> Path:
    """Export Teams chat messages.

    Args:
        days: Days of history to fetch
        output_dir: Directory for output file

    Returns:
        Path to exported JSON file
    """
    extractor = TeamsChatExtractor()
    output_dir = output_dir or _REPO_ROOT / "tools" / "exports"
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"teams_chat_{timestamp}.json"

    extractor.fetch_all_messages(days=days, output_path=output_path)
    print(f"Exported: {output_path}")

    return output_path
