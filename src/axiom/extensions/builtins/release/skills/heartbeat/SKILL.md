# heartbeat

Proactive CI sweep skill. Fired by launchd/systemd every
`heartbeat_interval` seconds (per the release extension's `[agent]`
block). Writes `~/.axi/agents/rivet/heartbeat.jsonl`.

This is the load-bearing dispatcher target for the release extension's
daemon loop — omitting or breaking its registration silently bricks
RIVET's CI watcher.

## Inputs

None. Invoked by the daemon scheduler with an empty params dict.

## Output

- Writes a JSONL heartbeat record under the user state dir.
- Returns `SkillResult(exit_code=0)` on success.

## Invocation

```bash
axi release heartbeat
```

Or programmatically via `SkillRegistry.invoke("release.heartbeat", {}, ctx)`.
