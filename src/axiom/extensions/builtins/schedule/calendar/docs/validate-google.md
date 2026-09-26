# Validating the Google Calendar integration

Legend: 🧑 = human step (you), 🤖 = step Claude runs, 💻 = local CLI.

---

## Track 0 — dead-simple (recommended): the `setup` utility does it all

The only irreducibly-manual part is the GCP key (create project + service account
+ download JSON — a browser login is unavoidable). After that, **one command**
provisions and verifies everything: it creates an **SA-owned calendar**, **shares
it back to you** (so it appears in your Google Calendar — no manual create, no
manual share), and runs a **live round-trip** to prove it.

```bash
cd /Users/example/Projects/workspace/axiom-pulse
pip install -e '.[calendar]'

# (optional) if you have gcloud authed, it even scripts the enable-API + SA + key:
PYTHONPATH=src python -m axiom.extensions.builtins.schedule.calendar.setup \
  bootstrap --project-id <your-project-id> --run

# the one command: provision + share-to-you + verify
PYTHONPATH=src python -m axiom.extensions.builtins.schedule.calendar.setup \
  provision --credentials-file ~/.config/axiom/pulse-calendar-sa.json \
            --share-with ben@b-treeventures.com
```
Expected:
```
✓ service account : axiom-pulse-calendar@axiom-support.iam.gserviceaccount.com
✓ created calendar : <id>  ("Axiom PULSE")
✓ shared with      : ben@b-treeventures.com  (it will appear in your Google Calendar)
✓ verified round-trip; next fires: <Mon>, <Wed>, <Fri>
Done — PULSE is connected to your calendar.
```

Stuck on access? `... setup doctor --credentials-file <json> --calendar-id <id>`
turns a raw 403/404 into the exact fix ("share calendar X with `<sa-email>`" /
"enable the API here").

The detailed tracks below are the manual fallback + how the MCP validation worked.

---

The manual tracks: **Track A** (MCP round-trip) is the fastest proof it works
using the session's already-authenticated Google connection. **Track B** (service
account, by hand) sets up the same auth the shipped provider uses.

---

## Track A — Fast round-trip via the Calendar MCP

### 🧑 A1. Create the dedicated test calendar
The MCP can manage events but not create calendars, so make it once by hand:
1. Open Google Calendar: <https://calendar.google.com/>
2. Left sidebar → **Other calendars** → **+** → **Create new calendar**
   (direct: <https://calendar.google.com/calendar/u/0/r/settings/createcalendar>)
3. Name it **`Axiom PULSE Test`** → **Create calendar**.
4. (Optional but recommended) set its time zone to UTC so round-trip times read cleanly.

That's the only human step Track A needs. Tell Claude it's done.

### 🤖 A2. Claude finds the calendar + runs the round-trip
Claude will then make these MCP calls (no further input needed):
- `list_calendars()` → locate the `Axiom PULSE Test` calendar id.
- `create_event(calendarId=<id>, summary="Axiom PULSE round-trip", start=..., end=..., recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"])`
- `get_event(...)` / `list_events(...)` → read it back and confirm Google returns the `recurrence` RRULE intact.
- Claude binds that event to an `rrule` cadence (`binding.event_to_cadence`) and prints the next 3 fire times, proving the mapping against *real* Google data.
- `delete_event(...)` → clean up the test event.

This validates the recurrence semantics end-to-end without any service-account setup.

---

## Track B — Production service account (what the shipped adapter uses)

Your test calendar lives in a **personal** Gmail account, so we use the
**share-with-service-account** pattern (no Workspace domain-wide delegation
needed). DWD notes for `b-treeventures.com` (Workspace) are at the end.

### 🧑 B1. GCP project + enable the Calendar API
1. Create or pick a project: <https://console.cloud.google.com/projectcreate>
2. Enable the Google Calendar API for it:
   <https://console.cloud.google.com/apis/library/calendar-json.googleapis.com>
   → **Enable**.

### 🧑 B2. Create a service account + key
1. Service accounts: <https://console.cloud.google.com/iam-admin/serviceaccounts>
   → **Create service account** (e.g. `axiom-pulse-calendar`). No project roles needed.
2. Open the SA → **Keys** → **Add key** → **Create new key** → **JSON** → download.
   Save it somewhere safe, e.g. `~/.config/axiom/pulse-calendar-sa.json`.
3. Copy the SA email (looks like `axiom-pulse-calendar@<project>.iam.gserviceaccount.com`).

### 🧑 B3. Share the test calendar with the service account
1. Google Calendar → hover **Axiom PULSE Test** → ⋮ → **Settings and sharing**
   (direct list: <https://calendar.google.com/calendar/u/0/r/settings>)
2. **Share with specific people or groups** → **Add people** → paste the SA email →
   permission **Make changes to events** → **Send**.
3. Copy the **Calendar ID** from the same page (**Integrate calendar** → *Calendar ID*,
   e.g. `…@group.calendar.google.com`).

### 💻 B4. Install the calendar extra + run the offline tests
```bash
cd /Users/example/Projects/workspace/axiom-pulse
# install the Google SDK extra (lazy-imported by the provider)
pip install -e '.[calendar]'        # or: uv pip install -e '.[calendar]'

# the network-free unit tests (mapping + CRUD against the fake service)
PYTHONPATH=src python -m pytest \
  src/axiom/extensions/builtins/schedule/tests/test_calendar.py \
  src/axiom/extensions/builtins/schedule/tests/test_google_calendar.py -q
```

### 💻 B5. Live round-trip against the real calendar
```bash
export GOOGLE_APPLICATION_CREDENTIALS=~/.config/axiom/pulse-calendar-sa.json
PYTHONPATH=src python -m axiom.extensions.builtins.schedule.calendar.validate_google \
  --calendar-id "<the Calendar ID from B3>"
```
Expected output: `detect -> configured`, an event created, its RRULE read back,
the bound cadence + next fire time, then `deleted (cleanup)` and `OK`.
Add `--keep` to leave the event in place to eyeball it in the Calendar UI.

---

## Workspace (domain-wide delegation) — for `ben@b-treeventures.com`

If/when you want the SA to act as a Workspace user instead of via calendar
sharing:
1. On the SA, enable **domain-wide delegation** and copy its **Client ID**.
2. Admin console → **Security → Access and data control → API controls →
   Domain-wide delegation**: <https://admin.google.com/ac/owl/domainwidedelegation>
   → **Add new** → the SA Client ID → scope `https://www.googleapis.com/auth/calendar`.
3. Run B5 with `--subject ben@b-treeventures.com`.

---

## M365 (next adapter — the hard creds you'll hunt down)

The M365 provider conforms to the same Protocol; only auth differs. You'll need:
- An **Entra app registration** (client id + tenant id):
  <https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade>
- A **client secret** (or certificate) on that app.
- **Application** Graph permission `Calendars.ReadWrite` + **admin consent**.
- The target user/mailbox or shared calendar id.
Tracked as blocked on the M365 Graph foundation (#366); hand these over and the
adapter wiring is mechanical.
