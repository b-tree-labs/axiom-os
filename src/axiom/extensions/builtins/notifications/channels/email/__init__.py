# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""HERALD ``email`` channel — nested Factory/Provider over cloud vendors.

Outer layer: ``EmailChannelAdapter`` + ``EmailChannelAdapterProvider``
plug into the HERALD ``ChannelAdapterRegistry`` as a single channel.

Inner layer: ``EmailProvider`` Protocol + ``register_email_provider``
factory. Each vendor backend (``smtp``, ``resend``, eventually
``microsoft365``, ``gmail``, ``ses``, ``sendgrid``, ``postmark``,
``mailgun``) lives in its own module and registers itself at import
time.

Adding a vendor = one new file under this package + an import of it
somewhere; no platform-code change. ``axi notifications channels list``
shows what the user actually has registered.
"""

from __future__ import annotations

# Backends register themselves at import time. Importing the package
# without these would leave the registry empty + every detect_provider
# call would return None.
from axiom.extensions.builtins.notifications.channels.email import (  # noqa: F401
    acs,
    gmail,
    resend,
    ses,
    smtp,
)
from axiom.extensions.builtins.notifications.channels.email.acs import (
    AcsEmailProvider,
)
from axiom.extensions.builtins.notifications.channels.email.base import (
    EmailMessage,
    EmailProvider,
    EmailSendResult,
)
from axiom.extensions.builtins.notifications.channels.email.channel import (
    EmailChannelAdapter,
    EmailChannelAdapterProvider,
    EmailDispatchResult,
)
from axiom.extensions.builtins.notifications.channels.email.factory import (
    detect_email_provider,
    email_provider_names,
    get_email_provider,
    register_email_provider,
)
from axiom.extensions.builtins.notifications.channels.email.gmail import (
    GmailEmailProvider,
)
from axiom.extensions.builtins.notifications.channels.email.resend import (
    ResendEmailProvider,
)
from axiom.extensions.builtins.notifications.channels.email.ses import (
    SesEmailProvider,
)
from axiom.extensions.builtins.notifications.channels.email.smtp import (
    SmtpConfig,
    SmtpEmailProvider,
)

__all__ = [
    "AcsEmailProvider",
    "EmailChannelAdapter",
    "EmailChannelAdapterProvider",
    "EmailDispatchResult",
    "EmailMessage",
    "EmailProvider",
    "EmailSendResult",
    "GmailEmailProvider",
    "ResendEmailProvider",
    "SesEmailProvider",
    "SmtpConfig",
    "SmtpEmailProvider",
    "detect_email_provider",
    "email_provider_names",
    "get_email_provider",
    "register_email_provider",
]
