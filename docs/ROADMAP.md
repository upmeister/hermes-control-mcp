# Roadmap

Hermes MCP Control Plane is entering its first public beta (`0.2.0b1`).

The roadmap is intentionally conservative: the bridge remains an MCP control plane over Hermes rather than becoming a second runtime or a generic multi-agent orchestration framework.

## Public beta foundation — complete

### Durable control plane

- idempotent `/v1/runs` submission and recovery;
- run status/wait/events;
- exact stop/steer;
- durable session history;
- explicit lane/request identity;
- multi-profile `/p/<profile>/...` routing with profile-scoped credentials;
- fail-closed cross-profile ambiguity/conflict handling.

### Shared live attach

- cooperative attach to an existing Hermes TUI/Desktop runtime;
- stored/runtime session identity split;
- live prompt/status/history/events/control;
- bounded reconnect/replay;
- conservative shared-turn attribution;
- actionable reconciliation after ambiguous outcomes;
- exact bounded retry for Hermes transient resume races.

Shared live attach remains **experimental / optional** because the currently deployed owner/native seam is not yet a stock Hermes public boundary.

### Public packaging

- `hermes-control-mcp` package and CLI;
- Python 3.11–3.13 CI;
- wheel + sdist build and clean installed-artifact MCP smoke;
- `doctor` readiness checks;
- registry schema v1;
- MIT license;
- compatibility/security/release documentation;
- PyPI Trusted Publishing release workflow.

Historical implementation contracts remain under `docs/STAGE-2.2*.md` for auditability.

## Near-term priorities

### R1 — Agent Sessions API parity research

Compare Hermes:

- `/v1/runs`;
- `/api/sessions/{id}/chat`;
- `/api/sessions/{id}/chat/stream`;
- TUI/native live attach.

The goal is to determine whether newer Hermes session APIs can simplify any durable bridge logic without weakening idempotency, detached execution, recovery, controls, or profile isolation.

See [API-SERVER-PARITY-SPIKE.md](API-SERVER-PARITY-SPIKE.md).

### R2 — upstream native/session authority watch

Track Hermes upstream work around:

- unified gateway session authority;
- supported local/native attach;
- ordinary prompt admission/turn identity;
- profile-scoped native authorization.

If Hermes lands a supported seam that supersedes the private owner adapter, prefer migration over growing a permanent bridge-specific fork.

See [UPSTREAM-HERMES.md](UPSTREAM-HERMES.md).

### P1 — interactive server requests

Approvals, clarify, sudo/secrets, vault unlock, and similar server→client requests require a separate security design.

Do not let one unattended model silently authorize privileged requests for another. Any implementation should expose explicit pending-request identity and answer/cancel methods.

### P2 — Streamable HTTP MCP

Useful for remote MCP clients, but it creates a new trust boundary:

- listener lifecycle;
- authentication;
- origin/network exposure;
- DoS/resource bounds;
- TLS/reverse-proxy guidance.

Stdio remains the default until this boundary is designed.

### P3 — cross-platform live IPC

Do not independently duplicate upstream local-authority work while Hermes is actively evolving it. Consume a stable upstream seam if one lands.

## Explicit non-goal: speculative Stage 3 A2A

Do not build broad peer/A2A orchestration merely because Hermes can be controlled through MCP.

Revisit only for a concrete use case that cannot be expressed through the control plane, profiles, or a supported Hermes upstream capability.
