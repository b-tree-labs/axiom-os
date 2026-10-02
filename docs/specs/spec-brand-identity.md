# Axiom Brand Identity

*Last updated: 2026-01-27*

## Positioning

### The Core Tension

| Audience | They Think About | They Don't Say |
|----------|------------------|----------------|
| System Operator | Situational awareness, shift turnover, compliance burden | "Digital twin" |
| Plant Manager | Capacity factor, regulatory body findings, workforce pipeline | "Machine learning" |
| regulatory body Inspector | 10 CFR 50.59 screens, audit trails, traceability | "AI agents" |
| Researcher | Novel methods, publications, funding | "Operations" |

### Market Evolution

```
Today (2026)           Near-term (2028)        Target (2030+)
─────────────────────────────────────────────────────────────
Research Platform  →   Pilot Deployments   →   Commercial Fleet
Universities           National Labs           Utilities
DOE/NEUP funding       GAIN vouchers           Subscription/SaaS
```

**Current reality:** Research platform proving concepts with facility, MSR experiments, bubble loop instrumentation.

**North star:** The operating system for commercial system operations—invisible infrastructure that makes operators smarter without asking them to become data scientists.

---

## Tagline Exploration

### Rejected

| Tagline | Why Not |
|---------|---------|
| "The auditable digital twin platform" | Tech speak; operators don't think in "twins" |
| "AI for domain-specific" | Vague; everyone claims this |
| "Nuclear digital transformation" | Consultant buzzwords |

### Candidates

| Tagline | Positioning | Audience |
|---------|-------------|----------|
| "System intelligence, always auditable" | Outcome + differentiator | Commercial |
| "From sensor to decision—trusted" | Data flow + trust | Operations |
| "The domain-specific operations platform" | Simple, direct | Universal |
| "Intelligence infrastructure for domain-specific" | Platform play | Enterprise |
| "Decisions at the speed of neutrons" | Evocative, memorable | Marketing |
| **"The intelligence platform for domain-specific power systems"** | **Adopted** | **Universal** |

### Working Tagline

> **"The intelligence platform for domain-specific power systems"**

Rationale:
- "Intelligence" signals decision support, not just data collection
- "Platform" conveys ecosystem/infrastructure, not point solution
- "Nuclear power systems" is specific to our target market (commercial fleet)
- Implies AI/ML capabilities without using buzzwords
- Works for both research and commercial contexts

---

## CLI Identity

### Command: `axi`

```bash
# Short, memorable, unique
$ axi

# Not neutr (incomplete), neutron (too long), ntn (unpronounceable)
```

### Mascot: The Newt 🦎

A newt (salamander) provides:
- **Visual pun:** axiom → newt
- **Personality:** Curious, adaptable, regenerative
- **Symbolism:** Salamanders historically associated with fire/transformation
- **Design flexibility:** Can show neutrons emanating from the newt

#### Mascot Variants

| Variant | Use Case |
|---------|----------|
| Newt with neutron orbits | Primary logo |
| Newt silhouette | Favicon, CLI spinner |
| Newt with hardhat | Operations/safety contexts |
| Newt with graduation cap | Learning/research contexts |
| Sleeping newt | Idle/waiting states |
| Alert newt | Notifications/warnings |

### Command Structure

```bash
axiom <domain> <action> [target] [flags]

# Examples
axiom sim run scenario.yaml          # Run simulation
axiom log query --last 1h            # Query ops log
axiom model list --type surrogate    # List ML models
axiom audit export --format nrc      # Export audit trail
axiom twin sync facility-001         # Sync digital twin state
axi teach neutronics               # Learning mode (ben-learning tie-in)
```

### Reserved Subcommands

| Command | Purpose |
|---------|---------|
| `axiom sim` | Simulation orchestration |
| `axiom model` | Surrogate/ML model management |
| `axiom log` | Ops log queries |
| `axiom audit` | Audit trail and compliance |
| `axiom twin` | Digital twin state management |
| `axiom data` | Data platform operations |
| `axiom infra` | Infrastructure management |
| `axiom teach` | Learning/education mode |
| `axiom agent` | AI agent interactions |
| `axiom ext` | Extension management (WASM plugins) |

---

## Naming Conventions

### Services

| Pattern | Example | Notes |
|---------|---------|-------|
| `axiom-<function>` | `axiom-gateway`, `axiom-log` | Public-facing services |
| `axi-<internal>` | `axi-scheduler`, `axi-cache` | Internal components |

### Data Assets

| Pattern | Example |
|---------|---------|
| `neos_<domain>_<entity>` | `neos_ops_log_entries` |
| `neos_<domain>_<entity>_<version>` | `neos_ml_surrogates_v2` |

### Code Packages

| Language | Pattern |
|----------|---------|
| Python | `axiom_<module>` |
| Rust | `axiom-<crate>` |
| TypeScript | `@axiom/<package>` |

---

## Voice & Tone

### Principles

1. **Trustworthy over clever** - Nuclear industry demands credibility
2. **Clear over comprehensive** - Operators are busy, respect their time
3. **Confident over hedging** - "Axiom provides X" not "Axiom can help with X"
4. **Technical over marketing** - Our audience detects BS immediately

### Examples

| ❌ Avoid | ✅ Prefer |
|----------|----------|
| "Leverage AI to optimize system performance" | "Surrogate models predict state 1000x faster than full simulation" |
| "Seamless integration" | "REST API. gRPC. OpenTelemetry. Pick your protocol." |
| "Enterprise-grade security" | "WASM sandboxing. Capability-based permissions. Deterministic replay." |
| "Digital transformation journey" | "Here's the API. Here's the audit trail. Ship it." |

---

## Visual Identity

### Color Palette (Proposed)

| Name | Hex | Use |
|------|-----|-----|
| Neutron Blue | `#1E3A5F` | Primary brand |
| System Cyan | `#00A9CE` | Accents, links |
| Caution Amber | `#F5A623` | Warnings |
| Critical Red | `#D0021B` | Errors, alerts |
| Success Green | `#7ED321` | Confirmations |
| Slate Gray | `#4A4A4A` | Body text |
| Paper White | `#FAFAFA` | Backgrounds |

### Typography

| Use | Font | Fallback |
|-----|------|----------|
| Headings | Inter | system-ui |
| Body | Inter | system-ui |
| Code | JetBrains Mono | monospace |

### Logo Concepts

```
┌─────────────────────────────────────┐
│                                     │
│     ○                               │
│    ╱│╲    ←── neutron orbits        │
│   ○─●─○                             │
│    ╲│╱                              │
│     ○                               │
│                                     │
│   🦎  ←── newt silhouette           │
│                                     │
│   N E U T R O N   O S               │
│                                     │
└─────────────────────────────────────┘
```

---

## Appendix: Name Origin

**Axiom** — The neutron is:
- The particle that sustains fission (the chain reaction)
- Electrically neutral (impartial, trustworthy)
- The messenger between nuclei (like our platform between systems)
- What operators actually care about (neutron flux, reactivity)

The name works because it's technically meaningful to the domain while being accessible to broader audiences.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
