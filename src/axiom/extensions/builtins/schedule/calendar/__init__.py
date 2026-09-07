# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Calendar protocol + provider factory for two-way calendar sync.

A calendar event is a scheduled thing — a time anchor + (optionally) a
recurrence + invitees. PULSE binds events to cadences: a recurring calendar
event *is* an iCalendar RRULE, which maps losslessly to an ``rrule`` cadence
(and back). Every vendor (Google first, then M365, CalDAV) implements one
``CalendarProvider`` Protocol; the factory hands back the right one.
"""

from axiom.extensions.builtins.schedule.calendar.factory import (
    available_vendors,
    detect,
    get_provider,
    register_vendor,
)
from axiom.extensions.builtins.schedule.calendar.protocol import (
    CalendarCapability,
    CalendarProvider,
    CapabilityUnsupported,
    EventRef,
    EventSpec,
)

# Register the bundled providers on import (google lazy-imports its SDK, so
# importing the module is safe even without google-api-python-client installed).
from axiom.extensions.builtins.schedule.calendar.vendors import fake as _fake  # noqa: E402,F401
from axiom.extensions.builtins.schedule.calendar.vendors import google as _google  # noqa: E402,F401
from axiom.extensions.builtins.schedule.calendar.vendors import m365 as _m365  # noqa: E402,F401
from axiom.extensions.builtins.schedule.calendar.vendors import caldav as _caldav  # noqa: E402,F401

__all__ = [
    "CalendarCapability",
    "CalendarProvider",
    "CapabilityUnsupported",
    "EventRef",
    "EventSpec",
    "available_vendors",
    "detect",
    "get_provider",
    "register_vendor",
]
