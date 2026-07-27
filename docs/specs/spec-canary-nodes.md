# Canary Nodes — Technical Specification

**Status:** Draft
**Date:** 2026-04-07
**PRD:** [Canary Nodes PRD](../requirements/prd-canary-nodes.md)
**Supersedes:** Original canary tech spec (same file, earlier revision)

---

## 1. Overview

This spec defines the implementation of the canary node protocol: how nodes detect edge releases, sandbox and smoke-test them, sign and push attestations, and how fleet nodes evaluate attestations to make local promotion decisions. All communication is push-based (outbound only) so canaries work behind firewalls.

---

## 2. Data Models

### 2.1 CanaryConfig

Stored in `~/.axi/config.toml` under the `[canary]` section:

```python
@dataclass
class CanaryConfig:
    """Configuration for a node operating as a canary."""

    name: str = ""                          # Axi character name (optional)
    check_interval: int = 900               # seconds between PyPI polls
    smoke_tier: int = 1                     # max smoke tier to run (1-4)
    packages: list[str] = field(
        default_factory=lambda: ["axi-platform"],
    )
    report_sinks: list[str] = field(        # where to push attestations
        default_factory=lambda: ["pack_server"],
    )  # Options: "pack_server", "github", "gossip", "webhook"
    webhook_url: str = ""                   # for webhook sink
    rollback_on_failure: bool = True
```

### 2.2 UpgradePolicy

Stored in `~/.axi/config.toml` under the `[upgrade]` section. Applies to ALL nodes (canary and fleet):

```python
@dataclass
class UpgradePolicy:
    """Local policy for deciding when to upgrade."""

    channel: str = "stable"                 # "edge" (canary) or "stable" (fleet)
    auto_upgrade: bool = True               # upgrade when criteria met
    min_canary_attestations: int = 3        # quorum size
    require_os_diversity: bool = True       # >= 2 OS families in quorum
    require_python_diversity: bool = False  # >= 2 Python versions in quorum
    require_matching_profile: bool = False  # canary like me must have passed
    silence_timeout_hours: int = 4          # no attestations = don't promote
    max_edge_age_hours: int = 72            # alert if still edge after this
```

### 2.3 CanaryAttestation

The core data structure. Signed by the canary's Ed25519 node key.

```python
@dataclass
class CanaryAttestation:
    """Signed attestation from a canary node about a release."""

    # Identity
    node_id: str                            # Ed25519-derived node ID
    canary_name: str                        # Axi name
    
    # Version
    version: str                            # version tested (e.g., "0.9.2")
    previous_version: str                   # version before upgrade
    
    # Result
    status: str                             # "green", "red", "rollback"
    failure_reason: str = ""                # empty if green
    
    # Security results (TRIAGE gate — runs before smoke)
    security_results: dict[str, SecurityCheckResult] = field(default_factory=dict)
    
    # Smoke results
    smoke_results: dict[str, TierResult] = field(default_factory=dict)
    upgrade_duration_seconds: int = 0
    
    # Platform profile (for diversity evaluation)
    os_family: str = ""                     # "linux", "darwin", "alpine"
    os_version: str = ""                    # "Ubuntu 24.04", "macOS 15.4"
    python_version: str = ""               # "3.12.8"
    infra_tier: str = ""                    # "k3d", "compose", "native", "sqlite"
    federation_role: str = ""               # "provider", "standard", "leaf"
    
    # Metadata
    timestamp: str = ""                     # ISO 8601
    nonce: str = ""                         # 16 random bytes (hex) — replay protection
    signature: str = ""                     # Ed25519 signature (base64)

    def signing_payload(self) -> bytes:
        """Canonical bytes for signing (all fields except signature)."""
        ...

    def verify(self, public_key: bytes) -> bool:
        """Verify signature against the given public key."""
        ...


@dataclass
class TierResult:
    passed: int
    failed: int
    duration_ms: int
    failures: list[str] = field(default_factory=list)  # names of failed tests
```

### 2.4 ReleaseChannelState

Each node tracks the promotion state of known releases:

```python
@dataclass
class ReleaseChannelState:
    """Local view of release promotion status."""

    version: str
    channel: str = "edge"                   # "edge" or "stable"
    first_seen: str = ""                    # when this node first learned of it
    attestations: list[CanaryAttestation] = field(default_factory=list)
    promoted_at: str = ""                   # when this node promoted it locally
    installed: bool = False
```

Persisted at `~/.axi/release-state.yaml`.

---

## 3. Canary Agent

The canary protocol runs as a RIVET extension. On canary nodes, RIVET's heartbeat loop includes the canary cycle.

### 3.1 CanaryAgent

```python
class CanaryAgent:
    """RIVET extension: detect, sandbox, smoke, attest, rollback."""

    def __init__(
        self,
        config: CanaryConfig,
        identity: NodeIdentity,
        sinks: list[AttestationSink],
    ):
        self.config = config
        self.identity = identity
        self.sinks = sinks

    async def check_cycle(self) -> CanaryAttestation | None:
        """Run one canary cycle. Returns attestation if new version found."""
        new_version = await self._detect_new_version()
        if new_version is None:
            return None

        attestation = await self._test_and_attest(new_version)
        await self._push_attestation(attestation)
        return attestation

    async def _detect_new_version(self) -> str | None:
        """Poll PyPI for versions newer than current install."""
        ...

    async def _test_and_attest(self, version: str) -> CanaryAttestation:
        """Sandbox → smoke → upgrade-or-rollback → build attestation."""
        staging = await self._create_staging_env(version)

        try:
            # Stage 1: install in sandbox
            await self._install_in_staging(staging, version)

            # Stage 2: smoke test in sandbox
            sandbox_result = await self._run_smoke(staging)
            if not sandbox_result.all_passed:
                return self._build_attestation(
                    version, status="red",
                    failure_reason=sandbox_result.summary,
                    smoke_results=sandbox_result.by_tier,
                )

            # Stage 3: commit upgrade to main install
            previous = self._current_version()
            await self._upgrade_main_install(version)

            # Stage 4: smoke test live install
            live_result = await self._run_smoke(live=True)
            if not live_result.all_passed:
                await self._rollback(previous)
                return self._build_attestation(
                    version, status="rollback",
                    failure_reason=live_result.summary,
                    smoke_results=live_result.by_tier,
                )

            return self._build_attestation(
                version, status="green",
                smoke_results=live_result.by_tier,
            )
        finally:
            await self._cleanup_staging(staging)

    async def _push_attestation(self, att: CanaryAttestation) -> None:
        """Sign and push to all configured sinks."""
        att.signature = self.identity.sign(att.signing_payload())
        for sink in self.sinks:
            try:
                await sink.push(att)
            except Exception:
                pass  # best-effort; log failure

    async def _rollback(self, previous_version: str) -> None:
        """Rollback to previous version and verify."""
        await self._install_version(previous_version)
        verify = await self._run_smoke(live=True, max_tier=1)
        if not verify.all_passed:
            # Critical: rollback failed. Alert operator immediately.
            raise CanaryRollbackFailed(previous_version)

    async def _create_staging_env(self, version: str) -> StagingEnv:
        """Create isolated venv for sandboxed testing."""
        staging_path = Path(tempfile.mkdtemp(prefix=f"canary-{version}-"))
        await asyncio.create_subprocess_exec(
            sys.executable, "-m", "venv", str(staging_path),
        )
        return StagingEnv(path=staging_path, version=version)

    async def _run_smoke(
        self,
        staging: StagingEnv | None = None,
        *,
        live: bool = False,
        max_tier: int | None = None,
    ) -> SmokeResult:
        """Run smoke tests up to configured tier."""
        tier = max_tier or self.config.smoke_tier
        python = str(staging.path / "bin" / "python") if staging else sys.executable
        results = {}
        for t in range(1, tier + 1):
            results[f"tier_{t}"] = await smoke_registry.run_tier(t, python=python)
        return SmokeResult(by_tier=results)
```

### 3.2 Smoke Test Registry

Tests are registered functions with tier annotations:

```python
smoke_registry = SmokeRegistry()

@smoke_registry.test(tier=1, name="cli_loads")
async def test_cli_loads(python: str) -> SmokeTestResult:
    result = await asyncio.create_subprocess_exec(
        python, "-m", "axiom.cli", "--help",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await result.wait()
    return SmokeTestResult(
        name="cli_loads",
        passed=result.returncode == 0,
    )

@smoke_registry.test(tier=1, name="extensions_discovered")
async def test_extensions(python: str) -> SmokeTestResult:
    """Verify builtin extensions load."""
    ...

@smoke_registry.test(tier=1, name="agent_patterns_load")
async def test_agent_patterns(python: str) -> SmokeTestResult:
    """Verify .axi/agents/ patterns load."""
    ...

@smoke_registry.test(tier=2, name="db_connects")
async def test_db_connects(python: str) -> SmokeTestResult:
    """Verify PostgreSQL connection."""
    ...

@smoke_registry.test(tier=2, name="model_roundtrip")
async def test_model_roundtrip(python: str) -> SmokeTestResult:
    """model init → validate → add → list → pull."""
    ...

@smoke_registry.test(tier=3, name="llm_responds")
async def test_llm_responds(python: str) -> SmokeTestResult:
    """Send simple prompt, verify response."""
    ...

@smoke_registry.test(tier=3, name="rag_search")
async def test_rag_search(python: str) -> SmokeTestResult:
    """RAG search returns results (if corpus indexed)."""
    ...

@smoke_registry.test(tier=4, name="federation_identity")
async def test_federation_identity(python: str) -> SmokeTestResult:
    """Node identity loads and federation status works."""
    ...
```

---

## 4. Attestation Sinks

All sinks implement the same interface. Canaries push to all configured sinks (best-effort).

```python
class AttestationSink(ABC):
    """Push attestations to an external store."""

    @abstractmethod
    async def push(self, attestation: CanaryAttestation) -> None: ...

    @abstractmethod
    async def list_attestations(self, version: str) -> list[CanaryAttestation]: ...
```

### 4.1 Pack Server Sink

Uses the existing pack server HTTP client. Attestations are stored alongside `.axiompack` files.

```python
class PackServerSink(AttestationSink):
    """Push attestations to the federation pack server."""

    async def push(self, attestation: CanaryAttestation) -> None:
        await self.client.post(
            f"/api/v1/attestations/{attestation.version}",
            json=attestation.to_dict(),
        )

    async def list_attestations(self, version: str) -> list[CanaryAttestation]:
        resp = await self.client.get(f"/api/v1/attestations/{version}")
        return [CanaryAttestation.from_dict(a) for a in resp.json()]
```

### 4.2 GitHub Sink

Posts attestations as comments on the GitHub Release, or as release assets.

```python
class GitHubSink(AttestationSink):
    """Push attestations to GitHub Release metadata."""

    async def push(self, attestation: CanaryAttestation) -> None:
        release = await self._get_release(attestation.version)
        await self.client.post(
            f"/repos/{self.repo}/releases/{release['id']}/assets",
            params={"name": f"canary-{attestation.node_id[:8]}.json"},
            content=json.dumps(attestation.to_dict()),
        )

    async def list_attestations(self, version: str) -> list[CanaryAttestation]:
        release = await self._get_release(version)
        assets = await self.client.get(
            f"/repos/{self.repo}/releases/{release['id']}/assets",
        )
        return [
            CanaryAttestation.from_dict(json.loads(a["content"]))
            for a in assets.json()
            if a["name"].startswith("canary-")
        ]
```

### 4.3 Gossip Sink

Writes attestation to local federation state. Propagates to peers on next sync cycle.

```python
class GossipSink(AttestationSink):
    """Store attestations in local federation state for peer propagation."""

    async def push(self, attestation: CanaryAttestation) -> None:
        state = self._load_state()
        state.setdefault(attestation.version, []).append(attestation.to_dict())
        self._save_state(state)
        # Attestations propagate to peers during regular federation sync

    async def list_attestations(self, version: str) -> list[CanaryAttestation]:
        state = self._load_state()
        return [
            CanaryAttestation.from_dict(a)
            for a in state.get(version, [])
        ]
```

### 4.4 Webhook Sink

Generic HTTP POST for integration with Slack, Teams, email gateways, etc.

```python
class WebhookSink(AttestationSink):
    """Push attestations to an arbitrary webhook URL."""

    async def push(self, attestation: CanaryAttestation) -> None:
        await self.client.post(self.url, json=attestation.to_dict())

    async def list_attestations(self, version: str) -> list[CanaryAttestation]:
        return []  # webhooks are write-only
```

---

## 5. Promotion Evaluator

Runs on every node (canary and fleet). Evaluates attestations against the local upgrade policy.

```python
class PromotionEvaluator:
    """Evaluate whether a release should be promoted locally."""

    def __init__(self, policy: UpgradePolicy, node_profile: NodeProfile):
        self.policy = policy
        self.profile = node_profile

    def evaluate(
        self,
        version: str,
        attestations: list[CanaryAttestation],
        first_seen: str,
    ) -> PromotionDecision:
        """Decide whether to promote a version from edge to stable."""
        green = [a for a in attestations if a.status == "green"]
        red = [a for a in attestations if a.status in ("red", "rollback")]

        # Check quorum
        if len(green) < self.policy.min_canary_attestations:
            return PromotionDecision(promote=False, reason="insufficient_quorum",
                detail=f"{len(green)}/{self.policy.min_canary_attestations} green")

        # Check OS diversity
        if self.policy.require_os_diversity:
            os_families = {a.os_family for a in green}
            if len(os_families) < 2:
                return PromotionDecision(promote=False, reason="insufficient_os_diversity",
                    detail=f"only {os_families}")

        # Check Python diversity
        if self.policy.require_python_diversity:
            py_versions = {a.python_version.rsplit(".", 1)[0] for a in green}
            if len(py_versions) < 2:
                return PromotionDecision(promote=False, reason="insufficient_python_diversity",
                    detail=f"only {py_versions}")

        # Check profile match
        if self.policy.require_matching_profile:
            matching = [
                a for a in green
                if a.os_family == self.profile.os_family
                and a.infra_tier == self.profile.infra_tier
            ]
            if not matching:
                return PromotionDecision(promote=False, reason="no_matching_profile",
                    detail=f"no canary matches {self.profile}")

        # Check for red attestations matching my profile
        profile_reds = [
            a for a in red
            if a.os_family == self.profile.os_family
            and a.infra_tier == self.profile.infra_tier
        ]
        if profile_reds:
            return PromotionDecision(promote=False, reason="profile_failure",
                detail=f"{len(profile_reds)} failures on matching profile")

        # Check silence timeout
        age = _hours_since(first_seen)
        if len(attestations) == 0 and age > self.policy.silence_timeout_hours:
            return PromotionDecision(promote=False, reason="silence_timeout",
                detail=f"no attestations after {age}h")

        return PromotionDecision(promote=True, reason="quorum_met",
            detail=f"{len(green)} green, {len(red)} red, "
                   f"{len({a.os_family for a in green})} OS families")


@dataclass
class PromotionDecision:
    promote: bool
    reason: str
    detail: str = ""
    override: bool = False          # True if promoted via human override
    override_by: str = ""           # node_id of the operator who overrode
    override_reason: str = ""       # free-text justification (required)
```

### 5.1 Manual Override

If quorum is never reached — canaries are down, the fleet is too small, there's a critical hotfix that can't wait — an operator can force promotion locally:

```bash
axi release promote 0.9.2 --force --reason "hotfix for CVE-2026-XXXX, single canary fleet"
```

This bypasses the quorum/diversity checks and promotes the version on **this node only**. It does NOT promote for the entire federation — each node is sovereign. The override is recorded in the release state and gossipped to peers as a special attestation:

```python
def force_promote(
    self,
    version: str,
    reason: str,
    operator_id: str,
) -> PromotionDecision:
    """Operator override — bypass quorum checks for this node."""
    if not reason:
        raise ValueError("--reason is required for manual override")
    return PromotionDecision(
        promote=True,
        reason="manual_override",
        detail=reason,
        override=True,
        override_by=operator_id,
        override_reason=reason,
    )
```

**Override rules:**
- `--reason` is **mandatory**. The system refuses to override without a justification.
- The override is logged to `~/.axi/logs/canary.log` and the audit trail.
- The override is gossipped to peers as an `operator_override` attestation — other nodes can see that someone forced a promotion, and decide independently whether to trust it.
- An override on one node does **not** override other nodes. Each operator must override their own node if they choose to.
- RIVET flags overrides in `axi release canary-status` so they're visible:

```
$ axi release canary-status

Release: axi-platform 0.9.2 (edge, 6h ago)
Promotion policy: 3 green, OS diversity required

Canary Attestations:
  ✅ VN-GO     macOS/3.14/K3D        green   2h ago
  ⏳ (no other canaries reporting)

Quorum: 1/3 green (need 2 more)
Promotion: BLOCKED — insufficient quorum

⚠️  Manual overrides:
  🔓 ben-laptop  "hotfix for CVE-2026-XXXX, single canary fleet"  1h ago
```

**When to override:**
- Critical security fix that can't wait for canary fleet to come online
- Bootstrap: federation has <3 nodes and quorum is mathematically impossible
- Canary infrastructure is down and the operator has tested manually
- Development/testing environments where canary gating adds friction

**When NOT to override:**
- "It's probably fine" — if you haven't tested, don't override
- To skip a red attestation — if a canary failed, investigate first

---

## 6. Fleet Upgrade Service

Runs on all nodes. Periodically collects attestations and evaluates promotion.

```python
class FleetUpgradeService:
    """Manages upgrades for non-canary (fleet) nodes."""

    def __init__(
        self,
        policy: UpgradePolicy,
        evaluator: PromotionEvaluator,
        sinks: list[AttestationSink],
    ):
        self.policy = policy
        self.evaluator = evaluator
        self.sinks = sinks

    async def check_cycle(self) -> None:
        """Check for upgradeable versions and apply if policy allows."""
        current = self._current_version()
        latest_edge = await self._detect_latest_edge()

        if latest_edge is None or latest_edge == current:
            return

        # Collect attestations from all readable sinks
        attestations = []
        for sink in self.sinks:
            try:
                attestations.extend(await sink.list_attestations(latest_edge))
            except Exception:
                continue  # best-effort collection

        # Deduplicate by node_id
        seen = set()
        unique = []
        for a in attestations:
            if a.node_id not in seen:
                seen.add(a.node_id)
                unique.append(a)

        state = self._load_release_state(latest_edge)
        decision = self.evaluator.evaluate(latest_edge, unique, state.first_seen)

        if decision.promote and self.policy.auto_upgrade:
            state.channel = "stable"
            state.promoted_at = datetime.utcnow().isoformat()
            self._save_release_state(state)
            await self._upgrade(latest_edge)
        elif not decision.promote:
            # Check for alerts
            age = _hours_since(state.first_seen)
            if age > self.policy.max_edge_age_hours:
                await self._alert_stale_edge(latest_edge, decision)
```

---

## 7. CLI

All commands are under `axi release`:

```
axi release canary-status              # Show canary attestation status for latest edge
axi release canary-run                 # Manually trigger one canary cycle
axi release canary-config              # Show canary configuration
axi release channel                    # Show current node's channel (edge/stable)
axi release policy                     # Show current upgrade policy
axi release attestations <version>     # List all attestations for a version
axi release promote <version>          # Manually evaluate promotion for a version
axi release promote <version> --force --reason "..."  # Override quorum (this node only)
```

### Example Output

```
$ axi release canary-status

Release: axi-platform 0.9.2 (edge, 2h 15m ago)
Promotion policy: 3 green, OS diversity required

Canary Attestations:
  ✅ VN-GO     macOS/3.14/K3D        green   45s upgrade   2h ago
  ✅ HOST-1    Ubuntu/3.12/K3D+GPU   green   62s upgrade   1h ago
  🔄 PRESS      Ubuntu/3.11/Compose   testing...
  ⏳ L-T       macOS/3.13/SQLite     pending (hasn't checked yet)
  ⏳ SUPPLY-R  Alpine/3.12/Compose   pending

Quorum: 2/3 green (need 1 more)
OS diversity: ✅ darwin, linux
Promotion: BLOCKED — insufficient quorum

$ axi release channel
Channel: stable
Auto-upgrade: enabled
Last upgrade: 0.9.1 (promoted 3d ago, 5 canary attestations)
```

---

## 8. A Self-Hosted Node as First Canary

A self-hosted GPU host (example-host) is the first production canary. It validates the protocol on real hardware behind a real firewall.

### 8.1 Node Profile

| Property | Value |
|----------|-------|
| **Node ID** | Ed25519-derived from the host's key |
| **Canary name** | HOST-1 (not an Axi name — it earned its own) |
| **OS** | Ubuntu 24.04 |
| **Python** | 3.12 |
| **Infra** | K3D + NVIDIA RTX PRO 6000 (97GB VRAM) |
| **Federation role** | Provider |
| **Smoke tier** | 4 (full suite — database, LLM, federation) |
| **Network** | Behind a private-network VPN. Outbound HTTPS works. No inbound from public internet. |

### 8.2 Node Canary Config

```toml
# ~/.axi/config.toml on the host

[node]
role = "canary"

[canary]
name = "HOST-1"
check_interval = 900          # 15 minutes
smoke_tier = 4                # full suite
packages = ["axi-platform", "domain-consumer"]
report_sinks = ["pack_server", "gossip"]
rollback_on_failure = true

[upgrade]
channel = "edge"
auto_upgrade = true
```

### 8.3 Why This Node Is an Ideal First Canary

1. **Behind a firewall** — validates the push-based attestation model. If it works from behind a private-network VPN, it works anywhere.
2. **GPU hardware** — validates LLM provider smoke tests (Qwen on RTX PRO 6000). Most CI systems can't test this.
3. **K3D + PostgreSQL** — validates the full infrastructure stack, not just pip install.
4. **Real federation peer** — has a trust relationship with Ben's laptop, so Tier 4 federation smoke tests actually test real peer communication.
5. **Always-on** — server hardware, not a laptop. Canary checks run 24/7 without human intervention.
6. **Different from CI** — CI tests source on ephemeral Ubuntu runners. This node tests wheels on persistent hardware with real state. They catch different bugs.

### 8.4 Node Deployment Steps

```bash
# On the host (via VPN SSH):

# 1. Upgrade to latest
pip install --upgrade axi-platform domain-consumer

# 2. Initialize federation identity (if not already done)
axi federation init

# 3. Configure as canary
axi config set node.role canary
axi config set canary.name HOST-1
axi config set canary.smoke_tier 4
axi config set canary.report_sinks '["pack_server", "gossip"]'
axi config set upgrade.channel edge

# 4. Verify canary config
axi release canary-config

# 5. Run first canary cycle manually
axi release canary-run

# 6. Enable cron (Phase 1) or RIVET heartbeat (Phase 2)
# Phase 1:
echo "*/15 * * * * $(which axi) release canary-run >> ~/.axi/canary.log 2>&1" | crontab -

# Phase 2 (when RIVET heartbeat is implemented):
# RIVET auto-runs canary cycles as part of its heartbeat loop
```

---

## 9. Security Considerations

The canary protocol has a dual security mandate: (1) verify the **package** being installed is authentic and untampered, and (2) verify the **attestations** being exchanged are genuine. Smoke tests check if a release *works*. Security checks verify it hasn't been *compromised*.

### 9.1 TRIAGE Integration — Package Verification

TRIAGE (30 existing tests: content verification, anomaly detection, trust scoring) runs as a **mandatory pre-smoke gate** in the canary pipeline. Before any smoke test executes, TRIAGE scans the staged package:

```python
async def _test_and_attest(self, version: str) -> CanaryAttestation:
    staging = await self._create_staging_env(version)
    try:
        await self._install_in_staging(staging, version)

        # --- SECURITY GATE (before smoke tests) ---
        security_result = await self._run_security_checks(staging, version)
        if not security_result.passed:
            return self._build_attestation(
                version, status="red",
                failure_reason=f"SECURITY: {security_result.summary}",
                smoke_results={},
                security_results=security_result,
            )

        # --- SMOKE TESTS (only if security passes) ---
        sandbox_result = await self._run_smoke(staging)
        ...
```

**TRIAGE checks performed on every canary cycle:**

| Check | What it does | Threat it mitigates |
|-------|-------------|-------------------|
| **Wheel hash verification** | Compare wheel SHA-256 against the hash published in our GitHub Release or a signed `CHECKSUMS.txt` | Tampered wheel on PyPI (supply chain attack, PyPI compromise) |
| **Dependency audit** | `pip audit` on the staging venv — check installed deps against known vulnerability databases (OSV, PyPI advisory) | Compromised or vulnerable transitive dependency |
| **Dependency pin drift** | Compare installed dependency versions against a pinned `requirements.lock` shipped with the release | Unpinned dependency silently upgraded to a malicious version |
| **Content scan** | TRIAGE content verifier scans installed `.py` files for injection patterns, obfuscated code, unexpected network calls, credential harvesting patterns | Malicious code injected into the package or a dependency |
| **Signature verification** | Verify the wheel's PEP 740 attestation (if available) or our own Ed25519 release signature | Package published by unauthorized party |
| **Import safety** | Run `import axiom` in a restricted subprocess with network disabled (if OS supports it) and check for unexpected side effects | Package that phones home or exfiltrates data on import |

```python
@dataclass
class SecurityResult:
    passed: bool
    checks: dict[str, SecurityCheckResult]  # check_name → result
    summary: str = ""

@dataclass
class SecurityCheckResult:
    name: str
    passed: bool
    severity: str = "info"          # info, warning, critical
    detail: str = ""
```

Security failures are **always RED and always block promotion** — there is no severity threshold. A security failure also triggers a RIVET escalation to the operator with the specific check that failed, because security failures are never routine.

The attestation includes security results so fleet nodes can see exactly what was verified:

```json
{
    "type": "canary_attestation",
    "version": "0.9.2",
    "status": "green",
    "security_results": {
        "wheel_hash": {"passed": true, "detail": "SHA-256 matches CHECKSUMS.txt"},
        "dependency_audit": {"passed": true, "detail": "0 known vulnerabilities"},
        "dependency_pin_drift": {"passed": true, "detail": "all deps match requirements.lock"},
        "content_scan": {"passed": true, "detail": "0 injection patterns, 0 obfuscation"},
        "signature_verification": {"passed": true, "detail": "Ed25519 release signature valid"},
        "import_safety": {"passed": true, "detail": "no unexpected side effects"}
    },
    "smoke_results": { ... }
}
```

### 9.2 Attestation Integrity

- Every attestation is signed with the canary's Ed25519 node key
- Fleet nodes verify signatures before counting attestations toward quorum
- Unknown node IDs are rejected (node must be in the local federation registry)
- Replay protection: attestations include a timestamp; nodes reject attestations older than `max_edge_age_hours`
- Attestations include a `nonce` field (random 16 bytes) to prevent identical attestations from being replayed even within the time window

### 9.3 Attestation Sink Integrity

A compromised attestation sink (pack server, GitHub) could serve fabricated or stale attestations. Mitigations:

- **Signature-first evaluation:** Fleet nodes verify Ed25519 signatures *before* evaluating attestations. A compromised sink can serve garbage, but it can't forge valid signatures without the canary's private key.
- **Multi-sink cross-check:** If a node reads from multiple sinks, it can cross-reference. An attestation that appears in gossip but not on the pack server (or vice versa) is flagged as suspicious.
- **Freshness check:** Attestations older than `max_edge_age_hours` are discarded regardless of signature validity. A compromised sink replaying old green attestations for a new version will fail because the version field won't match.

### 9.4 Staging Isolation

- Staging venvs are created in `/tmp` with unique names
- Staging tests run with the staged Python, not the system Python
- Staging venvs are cleaned up after every cycle (success or failure)
- Staging never modifies the running install, database, or federation state
- Security checks (9.1) run in the staging venv, so a malicious package is contained even if it tries to execute during scanning

### 9.5 Supply Chain

- Canaries install from PyPI (HTTPS, TLS-verified)
- Wheel hashes verified against a signed `CHECKSUMS.txt` published with each release (Section 9.1)
- The `SUPPLY-R` canary specifically validates minimal-OS installs (Alpine) to catch missing native dependencies or platform-specific supply chain issues
- Future: support for private PyPI mirrors for air-gapped deployments, with mirror-specific `CHECKSUMS.txt`
- The CI/CD pipeline (GitHub Actions) publishes `CHECKSUMS.txt` as a release asset, signed with the project's release key. This is the root of trust for wheel verification.

### 9.6 Trust Boundaries

- Only attestations from nodes in the local federation registry are considered
- A compromised canary can only attest for itself — it cannot forge attestations from other nodes
- A single red attestation from a matching-profile canary blocks promotion for nodes with `require_matching_profile = true`
- **Security failures propagate loudly:** a RED attestation with `SECURITY:` prefix triggers RIVET alerts on every node that receives it, not just the canary that detected it
- Worst case: all canaries compromised → false green attestations → fleet upgrades to a bad version. Mitigations:
  - Fleet nodes can optionally run their own security checks post-upgrade (`[upgrade] verify_after_install = true`)
  - Fleet nodes roll back independently if their own post-install verification fails
  - TRIAGE content scanning on canaries catches most code-level tampering before attestation

### 9.7 Release Signing — Root of Trust

The canary protocol's security ultimately rests on two signing keys:

| Key | Purpose | Holder | Protects against |
|-----|---------|--------|-----------------|
| **Project release key** (Ed25519) | Signs `CHECKSUMS.txt` with wheel hashes | CI/CD pipeline (GitHub Actions secret) | Tampered wheels on PyPI |
| **Node key** (Ed25519) | Signs canary attestations | Each individual node | Forged attestations |

The release key is the root of trust for package integrity. The node key is the root of trust for attestation integrity. They are independent — compromising one does not compromise the other.

---

## 10. Observability

### 10.1 Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `axiom_canary_check_total` | counter | Total canary check cycles |
| `axiom_canary_upgrade_total` | counter | Total upgrades attempted (by status: green/red/rollback) |
| `axiom_canary_smoke_duration_seconds` | histogram | Smoke test duration by tier |
| `axiom_canary_attestation_push_total` | counter | Attestations pushed (by sink, by status) |
| `axiom_fleet_promotion_total` | counter | Releases promoted locally |
| `axiom_fleet_promotion_latency_seconds` | histogram | Time from edge publish to local promotion |

### 10.2 Logs

All canary operations log to `~/.axi/logs/canary.log`:

```
2026-04-07T18:00:00Z INFO  canary: checking PyPI for axi-platform updates
2026-04-07T18:00:01Z INFO  canary: new version detected: 0.9.2 (current: 0.9.1)
2026-04-07T18:00:01Z INFO  canary: creating staging env at /tmp/canary-0.9.2-xxxxx
2026-04-07T18:00:15Z INFO  canary: staging install complete
2026-04-07T18:00:15Z INFO  canary: running smoke tier 1 in staging
2026-04-07T18:00:17Z INFO  canary: tier 1 passed (6/6, 1.2s)
2026-04-07T18:00:17Z INFO  canary: running smoke tier 2 in staging
2026-04-07T18:00:21Z INFO  canary: tier 2 passed (4/4, 3.4s)
2026-04-07T18:00:21Z INFO  canary: upgrading main install to 0.9.2
2026-04-07T18:00:35Z INFO  canary: main install upgraded, re-running smoke
2026-04-07T18:01:00Z INFO  canary: all tiers passed. attesting GREEN
2026-04-07T18:01:01Z INFO  canary: attestation pushed to pack_server
2026-04-07T18:01:01Z INFO  canary: attestation pushed to gossip (2 peers)
```

---

## 11. File Layout

```
src/axiom/
├── canary/
│   ├── __init__.py
│   ├── agent.py              # CanaryAgent — detect, sandbox, smoke, attest
│   ├── attestation.py        # CanaryAttestation, TierResult, signing/verification
│   ├── config.py             # CanaryConfig, UpgradePolicy
│   ├── evaluator.py          # PromotionEvaluator, PromotionDecision
│   ├── fleet.py              # FleetUpgradeService
│   ├── smoke.py              # SmokeRegistry, SmokeTestResult, built-in tests
│   └── sinks/
│       ├── __init__.py
│       ├── base.py           # AttestationSink ABC
│       ├── pack_server.py    # PackServerSink
│       ├── github.py         # GitHubSink
│       ├── gossip.py         # GossipSink
│       └── webhook.py        # WebhookSink
├── extensions/builtins/
│   └── release/
│       └── cli.py            # axi release canary-* commands
```

---

## 12. Dependencies

| Dependency | Purpose | Already in project? |
|------------|---------|-------------------|
| `cryptography` (Ed25519) | Attestation signing/verification | Yes (federation identity) |
| `httpx` | Async HTTP for sinks + PyPI polling | Yes (pack_server client) |
| `pyyaml` | Release state persistence | Yes |

No new dependencies required.

---

## 13. Test Plan

| Test | What it validates |
|------|------------------|
| `test_canary_detect_new_version` | PyPI polling detects new version correctly |
| `test_canary_staging_isolation` | Staging venv is isolated from main install |
| `test_canary_smoke_tiers` | Each tier runs correct tests, respects max tier |
| `test_canary_green_attestation` | Green smoke → signed green attestation |
| `test_canary_red_attestation` | Failed smoke → signed red attestation, no upgrade |
| `test_canary_rollback` | Failed post-upgrade smoke → rollback + verify |
| `test_attestation_signing` | Sign → serialize → deserialize → verify round-trip |
| `test_attestation_reject_invalid_sig` | Tampered attestation rejected |
| `test_promotion_quorum` | N green attestations → promote |
| `test_promotion_insufficient_quorum` | N-1 green → don't promote |
| `test_promotion_os_diversity` | All-same-OS quorum rejected when diversity required |
| `test_promotion_profile_match` | Missing matching profile blocks promotion |
| `test_promotion_profile_red` | Red on matching profile blocks promotion |
| `test_promotion_silence_timeout` | No attestations after timeout → don't promote |
| `test_fleet_auto_upgrade` | Promotion met + auto_upgrade → install |
| `test_fleet_manual_only` | Promotion met + auto_upgrade=false → don't install |
| `test_sink_pack_server` | Push/list round-trip via pack server |
| `test_sink_github` | Push/list round-trip via GitHub releases |
| `test_sink_gossip` | Push → propagate to peer → list on peer |
| `test_security_wheel_hash_valid` | Correct wheel hash passes verification |
| `test_security_wheel_hash_tampered` | Wrong hash → RED, blocks smoke tests |
| `test_security_dep_audit_clean` | No known vulnerabilities → pass |
| `test_security_dep_audit_vuln` | Known vulnerability → RED |
| `test_security_content_scan_clean` | Clean package passes TRIAGE scan |
| `test_security_content_scan_injection` | Injected malicious pattern → RED |
| `test_security_blocks_smoke` | Security failure prevents smoke tests from running |
| `test_security_failure_propagates` | RED security attestation triggers RIVET alert on receiving nodes |
| `test_attestation_nonce_replay` | Same attestation replayed with same nonce rejected |
| `test_attestation_stale_rejected` | Attestation older than max_edge_age rejected |
| `test_sink_cross_check` | Attestation in one sink but not another flagged suspicious |

---

## 14. Phasing

### Phase 1 (current + 1 release)

Implement:
- `CanaryConfig`, `UpgradePolicy`, `CanaryAttestation` data models
- `SmokeRegistry` with Tier 1 tests
- `CanaryAgent._detect_new_version()` (PyPI polling)
- `CanaryAgent._create_staging_env()` and `_run_smoke()`
- `GossipSink` (simplest sink — local file)
- `axi release canary-run` and `axi release canary-status` CLI
- A self-hosted node configured as first canary with cron

Skip:
- PackServerSink, GitHubSink (need server-side endpoint)
- PromotionEvaluator (manual promotion in Phase 1)
- FleetUpgradeService (manual upgrades in Phase 1)

### Phase 2 (v1.2)

Add:
- Full `PromotionEvaluator` with policy enforcement
- `FleetUpgradeService` with auto-upgrade
- `PackServerSink` with server-side attestation storage
- Tier 2-3 smoke tests
- RIVET heartbeat integration (replaces cron)

### Phase 3 (v1.3)

Add:
- `GitHubSink` for open-source attestation
- CI-triggered canary matrix (GitHub Actions)
- Tier 4 federation smoke tests
- `WebhookSink` for external integrations

### Phase 4 (v2.0+)

Add:
- Commercial canary opt-in (license flag)
- Canary telemetry dashboard
- Cross-federation attestation sharing
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
