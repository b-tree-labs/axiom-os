# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the Outlook Email channel.

NOT YET IMPLEMENTED — Email ingestion requires either:
  a) MS Graph API with Mail.Read scope (preferred), or
  b) An IMAP/SMTP integration

This file documents the planned architecture and serves as
the skeleton for when the extractor is built.

Planned flow:
  1. MS Graph: poll inbox for emails matching a filter/label
  2. Extract body text + attachments
  3. Route text through freetext extractor
  4. Route attachments through appropriate extractors (.m4a → voice, etc.)

See: runtime/config.example/facility.toml
     [sense.sources] email_forwarding = false
"""

import pytest

pytestmark = [pytest.mark.integration]


class TestOutlookEmail:
    """Placeholder for Outlook email channel."""

    def test_not_yet_implemented(self):
        """Email extractor does not exist yet."""
        pytest.skip(
            "Outlook email extractor not yet implemented. "
            "Needs: MS Graph API + Mail.Read scope + new extractor in "
            "src/axiom/extensions/builtins/signals/extractors/email.py. "
            "Enable in facility.toml: [sense.sources] email_forwarding = true"
        )

    def test_ms_graph_mail_scope(self, ms_graph_creds):
        """When implemented: verify MS Graph can read mail."""
        pytest.skip("Email extractor not yet implemented")
