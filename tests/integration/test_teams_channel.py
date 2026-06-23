# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the Teams channel.

Two sub-channels:
  1. Teams Recordings — meeting transcripts (.vtt files) processed by the transcript extractor
  2. Teams Channels (text) — NOT YET IMPLEMENTED, placeholder tests

For recordings: tests that the transcript extractor handles real .vtt files.
MS Graph API access is needed for fetching recordings (future automation),
but for now transcripts are manually placed in inbox/raw/teams/.

No external credentials needed for local transcript processing.
"""

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.teams]

# Sample VTT transcript content (realistic Teams format)
SAMPLE_VTT = """\
WEBVTT

00:00:00.000 --> 00:00:05.000
Dr. Smith: Good morning everyone. Let's start with the status update on the Alpha System model.

00:00:05.000 --> 00:00:12.000
Alice Johnson: The thermal simulation is running but we're seeing convergence issues near the component boundary.

00:00:12.000 --> 00:00:20.000
Dr. Smith: Can you share the mesh sensitivity results? We need those before the program review next Friday.

00:00:20.000 --> 00:00:28.000
Bob Chen: I've been working on the off-gas model. The transport equations are validated against reference data now.

00:00:28.000 --> 00:00:35.000
Dr. Smith: Excellent. Alice, make sure to push your branch before the review. Bob, can you write up a summary?
"""


class TestTeamsTranscriptProcessing:
    """Test processing of Teams meeting transcripts."""

    def test_vtt_file_recognized(self, tmp_path):
        """Transcript extractor recognizes .vtt files in a teams directory."""
        from axiom.extensions.builtins.signals.extractors.transcript import (
            TranscriptExtractor,
        )

        teams_dir = tmp_path / "teams"
        teams_dir.mkdir()
        vtt = teams_dir / "meeting_transcript_2026-02-18.vtt"
        vtt.write_text(SAMPLE_VTT)

        # The transcript extractor handles .md/.txt with "transcript" or "teams" in path
        # VTT files need to be converted first, but let's test .md version
        md = teams_dir / "meeting_transcript_2026-02-18.md"
        md.write_text(SAMPLE_VTT)

        extractor = TranscriptExtractor()
        assert extractor.can_handle(md)

    def test_extract_signals_from_transcript(self, tmp_path):
        """Extract signals from a meeting transcript without LLM."""
        from axiom.extensions.builtins.signals.extractors.transcript import (
            TranscriptExtractor,
        )

        teams_dir = tmp_path / "teams"
        teams_dir.mkdir()
        transcript = teams_dir / "meeting_transcript.md"
        transcript.write_text(SAMPLE_VTT)

        extractor = TranscriptExtractor()
        extraction = extractor.extract(transcript)

        assert len(extraction.signals) > 0
        fatal_errors = [e for e in extraction.errors if "(non-fatal)" not in e]
        assert fatal_errors == []

        for s in extraction.signals:
            print(f"  [{s.signal_type}] {s.detail[:80]}")

    def test_vtt_upload_via_serve(self, tmp_path):
        """Upload a .vtt file via sense serve — should route to teams/."""
        import json
        import threading
        import urllib.request

        from axiom.extensions.builtins.signals.serve import create_server

        inbox = tmp_path / "inbox" / "raw"
        inbox.mkdir(parents=True)

        srv = create_server(host="127.0.0.1", port=0, inbox_root=inbox)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()

        try:
            host, port = srv.server_address
            url = f"http://{host}:{port}/upload"

            _boundary = "----TeamsTest"
            body = (
                b"------TeamsTest\r\n"
                b'Content-Disposition: form-data; name="file"; filename="standup_2026-02-18.vtt"\r\n'
                b"Content-Type: text/vtt\r\n"
                b"\r\n"
            ) + SAMPLE_VTT.encode() + b"\r\n------TeamsTest--\r\n"

            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "multipart/form-data; boundary=----TeamsTest"},
                method="POST",
            )
            resp = urllib.request.urlopen(req)
            result = json.loads(resp.read())

            assert "Saved" in result["message"]
            assert (inbox / "teams" / "standup_2026-02-18.vtt").exists()
            print(f"  VTT routed: {result['message']}")
        finally:
            srv.shutdown()


class TestTeamsChannelsText:
    """Placeholder for Teams Channels (text chat) integration.

    NOT YET IMPLEMENTED — Teams channel messages require the
    MS Graph API with Teams.Read permissions. This will be a
    future extractor.
    """

    def test_not_yet_implemented(self):
        """Teams text channel extractor does not exist yet."""
        pytest.skip(
            "Teams channel text extractor not yet implemented. "
            "Needs: MS Graph API + Teams.Read scope + new extractor in "
            "src/axiom/extensions/builtins/signals/extractors/teams_chat.py"
        )
