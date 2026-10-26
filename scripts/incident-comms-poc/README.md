# Human↔Agent comms POC — run it for real on Slack

Proves the scenario end-to-end on a real Slack workspace: TRIAGE posts an OOM
incident → you quiz it in-thread → Approve → TIDY applies + reports.

Everything that *can* be automated is. The only steps Slack forces a human to
do are **(2) consent to install the app** and **(3) copy the two tokens** — and
those are reduced to clicks/paste.

Run from this worktree dir: `the axiom-incident-comms worktree`.

## 0. One-time deps
```bash
../.venv/bin/pip install slack_sdk            # the only extra dependency
```

## 1. Create the Slack app (scripted)
```bash
../.venv/bin/python scripts/incident-comms-poc/make_slack_app.py --channel ops-sysadmin
```
- Prints the full app manifest + a **"create from manifest"** deep link. Open it → review → **Create**.
- Zero-click variant: generate an App-Configuration token once at
  api.slack.com/apps → *Your App Configuration Tokens*, then:
  ```bash
  ../.venv/bin/python scripts/incident-comms-poc/make_slack_app.py --channel ops-sysadmin --config-token xoxe-...
  ```
  This creates the app via `apps.manifest.create` and prints the install link.

## 2. Install to workspace + invite the bot (the human-mandated step)
- Open the printed install link (or OAuth & Permissions → **Install to Workspace**) → **Allow**.
- In Slack, create/open `#ops-sysadmin` and `/invite` the bot.

## 3. Capture the two tokens (paste, never echoed into git)
- **Bot token** (`xoxb-…`): OAuth & Permissions → Bot User OAuth Token.
- **App-level token** (`xapp-…`): Basic Information → App-Level Tokens → generate with `connections:write` (Socket Mode).
```bash
cp scripts/incident-comms-poc/env.example scripts/incident-comms-poc/.env          # scripts/incident-comms-poc/.env is gitignored
$EDITOR scripts/incident-comms-poc/.env                     # paste the two tokens + channel id
```

## 4. Run the live proof
```bash
set -a; . scripts/incident-comms-poc/.env; set +a
../.venv/bin/python scripts/incident-comms-poc/run_poc.py
```
You'll see in `#ops-sysadmin`:
1. an **incident brief** (what broke, 7059 restarts, root cause, the reversible 1.5→16 GiB fix) + **Approve / Deny** buttons;
2. ask in-thread — *"what's the current limit?"*, *"is it reversible?"*, *"why is it crash-looping?"* — TRIAGE answers from the incident context;
3. click **Approve & apply** → TIDY applies (demo) and posts the verified outcome; **Deny** → no change.

### Make the remediation real (optional)
Default is a safe demo apply. To apply a real, reversible limit bump on a
reachable cluster (e.g. on a self-hosted node, the actual langfuse ClickHouse fix):
```bash
export AXI_POC_APPLY=kubectl AXI_POC_KCTL_NS=langfuse AXI_POC_KCTL_STS=langfuse-clickhouse-shard0
../.venv/bin/python scripts/incident-comms-poc/run_poc.py
```

## What this proves
The vendor-neutral `InteractiveChannel` + `IncidentConversation` drive the
whole loop; **Slack is just a provider**. Swap in a Teams provider later and
this exact driver works unchanged — the foundation for agents in every channel.
