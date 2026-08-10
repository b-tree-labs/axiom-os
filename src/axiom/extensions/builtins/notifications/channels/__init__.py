# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""HERALD channel-adapter machinery.

HERALD-2a (2026-06-01) ships the first cluster of real outbound channels
behind the Provider/Factory shape:

- ``slack`` (incoming webhook)
- ``mattermost`` (incoming webhook, self-hosted-friendly)
- ``email`` — **nested Factory/Provider** over cloud email vendors
  (SMTP + Resend ship now; M365 Graph, Gmail API, SES, SendGrid,
  Postmark, Mailgun follow as self-registering modules under
  ``channels/email/``)

Teams + bidirectional Events-API / IMAP-poll ingest are HERALD-2b.
"""

from axiom.extensions.builtins.notifications.channels.base import (
    ChannelAdapter,
    ChannelAdapterProvider,
    ChannelAdapterRegistry,
    ChannelCapabilities,
    Direction,
)
from axiom.extensions.builtins.notifications.channels.email import (
    EmailChannelAdapter,
    EmailChannelAdapterProvider,
    EmailMessage,
    EmailProvider,
    EmailSendResult,
)
from axiom.extensions.builtins.notifications.channels.inbox import (
    InboxChannelAdapter,
    InboxChannelAdapterProvider,
)
from axiom.extensions.builtins.notifications.channels.mattermost import (
    MattermostChannelAdapter,
    MattermostChannelAdapterProvider,
)
from axiom.extensions.builtins.notifications.channels.slack import (
    SlackChannelAdapter,
    SlackChannelAdapterProvider,
)
from axiom.extensions.builtins.notifications.channels.teams import (
    TeamsChannelAdapter,
    TeamsChannelAdapterProvider,
    TeamsDispatchResult,
)
from axiom.extensions.builtins.notifications.channels.twilio_sms import (
    TwilioSmsChannelAdapter,
    TwilioSmsChannelAdapterProvider,
    TwilioSmsDispatchResult,
)

__all__ = [
    "ChannelAdapter",
    "ChannelAdapterProvider",
    "ChannelAdapterRegistry",
    "ChannelCapabilities",
    "Direction",
    "EmailChannelAdapter",
    "EmailChannelAdapterProvider",
    "EmailMessage",
    "EmailProvider",
    "EmailSendResult",
    "InboxChannelAdapter",
    "InboxChannelAdapterProvider",
    "MattermostChannelAdapter",
    "MattermostChannelAdapterProvider",
    "SlackChannelAdapter",
    "SlackChannelAdapterProvider",
    "TeamsChannelAdapter",
    "TeamsChannelAdapterProvider",
    "TeamsDispatchResult",
    "TwilioSmsChannelAdapter",
    "TwilioSmsChannelAdapterProvider",
    "TwilioSmsDispatchResult",
]
