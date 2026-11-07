# Microsoft 365 (Graph) calendar — setup, for people who don't admin M365

The M365 provider is the same `CalendarProvider` as Google; only the auth setup
is intimidating. Two things make it painless:

1. **You don't need your corporate tenant.** A free **Microsoft 365 Developer
   tenant** makes *you* the global admin of a throwaway sandbox (25 test
   mailboxes) — so "Grant admin consent" is one click you own, no IT ticket.
   Sign up: <https://developer.microsoft.com/microsoft-365/dev-program>
2. **The doctor tells you exactly what's missing** after each portal step, so a
   raw `403` never leaves you stuck:
   ```
   python -m axiom.extensions.builtins.schedule.calendar.setup m365-doctor \
     --tenant-id <T> --client-id <C> --client-secret <S> --user-id <mailbox-upn>
   ```
   It prints a precise `→ NEXT:` (wrong secret? consent missing? wrong mailbox?).

Run `pip install -e '.[m365]'` first.

Legend: 🧑 = you in a portal, 💻 = local command + doctor checkpoint.

---

### 🧑 1. (Recommended) get a Developer tenant you fully admin
<https://developer.microsoft.com/microsoft-365/dev-program> → **Join** → set up
the **instant sandbox**. You get a tenant like `you@<name>.onmicrosoft.com` where
**you are Global Administrator**. Use one of its mailboxes as `--user-id`.
*(Skip this only if you already have admin on a real tenant.)*

### 🧑 2. Register an app
<https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade>
→ **New registration** → name `axiom-pulse-calendar` → **Register**.
On the app's **Overview**, copy:
- **Application (client) ID** → `--client-id`
- **Directory (tenant) ID** → `--tenant-id`

### 🧑 3. Add a client secret
App → **Certificates & secrets** → **New client secret** → copy the **Value**
(the long string, *not* the "Secret ID") → `--client-secret`. It's shown once.

💻 **Checkpoint:** run `m365-doctor` now. If auth is wrong you'll see
`→ NEXT: Wrong tenant id…` / `…client secret…` / `…client id…` — fix and re-run
until the error becomes a *consent* message (that means auth works).

### 🧑 4. Add the permission + grant admin consent
App → **API permissions** → **Add a permission** → **Microsoft Graph** →
**Application permissions** (not Delegated) → check **`Calendars.ReadWrite`** →
**Add permissions**. Then click **Grant admin consent for <tenant>** and confirm
(the row turns green ✓). On a dev tenant, that's you — one click.

💻 **Checkpoint:** `m365-doctor` should now print `✓ Graph calendar reachable.`
If it still says *consent*, the grant didn't take (wait ~1 min, re-grant). If it
says *mailbox*, the `--user-id` UPN is wrong or has no Exchange mailbox.

### 💻 5. Make it real — one command
```bash
python -m axiom.extensions.builtins.schedule.calendar.setup m365-provision \
  --tenant-id <T> --client-id <C> --client-secret <S> --user-id <mailbox-upn>
```
This creates an **"Axiom PULSE"** calendar *in that mailbox* (no sharing step —
it's the user's own mailbox) and runs a live round-trip:
```
✓ created calendar : <id>  ("Axiom PULSE") in <mailbox>'s mailbox
✓ verified round-trip; next fires: <Mon>, <Wed>, <Fri>
Done — PULSE is connected to your M365 calendar.
```

---

## What each doctor verdict means
| `→ NEXT:` says | Do this |
|---|---|
| Wrong tenant / client id | re-copy from Entra → app → **Overview** |
| Wrong/expired client secret | new secret; copy the **Value**, not the Secret ID |
| Grant admin consent / Calendars.ReadWrite | step 4 — **Application** permission + the green consent ✓ |
| mailbox not reachable | `--user-id` must be a real UPN with an Exchange mailbox in this tenant |

## Least-privilege (optional, for a real tenant later)
By default an app permission can read every mailbox in the tenant. Scope it to
the one calendar mailbox with an **application access policy** (Exchange Online
PowerShell `New-ApplicationAccessPolicy`). Not needed for a dev-tenant sandbox.
