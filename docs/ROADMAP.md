# Roadmap

Hermes MCP Control Plane published its first public beta (`0.2.0b1`) on 2026-09-20.

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
- PyPI Trusted Publishing release workflow;
- first public prerelease `v0.2.0b1` with wheel + sdist attached to GitHub Release.

Historical implementation contracts remain under `docs/STAGE-2.2*.md` for auditability.

## Immediate 0.2.x stabilization lane

Before broadening the protocol surface, keep a small maintenance lane open for public-beta feedback:

- fix reproducible packaging/install/doctor regressions as patch/beta releases;
- keep compatibility notes aligned with the latest supported Hermes release;
- preserve the stable durable-vs-experimental-live capability split;
- prefer regression tests for every public issue that crosses request/profile/session identity boundaries;
- do not turn public-beta feedback into speculative feature creep.

This lane runs in parallel with the research items below and does not block low-risk analysis work.

## First queued implementation: UX1 — setup and client-config onboarding

Before broader protocol work, remove avoidable onboarding friction from the
current stdio release.

Target user journey:

~~~text
install -> doctor -> generate client config -> connect
~~~

The first UX patch should add a non-mutating setup/config generator, for example:

~~~text
hermes-control-mcp client-config zcode
hermes-control-mcp client-config cursor
hermes-control-mcp client-config codex
hermes-control-mcp client-config vscode
hermes-control-mcp client-config <client> --ssh <hermes-host>
~~~

Exact command naming can be finalized during implementation, but the contract
should be:

- detect/report the same-host happy path without requiring users to understand
  `--api-url`, `--env-file`, or `--profiles-root`;
- emit the exact host-specific config shape for major MCP clients rather than
  calling one JSON schema generic;
- generate the current SSH-launch form for remote stdio clients without copying
  Hermes profile secrets to the client machine;
- choose or suggest a safe per-client state DB path;
- reuse `doctor` readiness/config discovery instead of duplicating it;
- print proposed configuration by default; do not silently mutate third-party
  client config files;
- keep credentials out of generated command arguments and tracked files.

Documentation and implementation should be designed together so the generated
output remains the canonical source for examples rather than hand-maintained
snippets drifting across clients.

This is deliberately a small UX layer over the existing trust model, not a new
remote transport.

## Next substantive priority: R1 — Agent Sessions API parity research

Compare Hermes:

- `/v1/runs`;
- `/api/sessions/{id}/chat`;
- `/api/sessions/{id}/chat/stream`;
- TUI/native live attach.

The goal is to determine whether newer Hermes session APIs can simplify any durable bridge logic without weakening:

- idempotency;
- detached execution;
- ambiguous-submit recovery;
- exact controls;
- event/replay semantics;
- profile isolation;
- compatibility with current public MCP tools.

This is the first substantive post-beta task because it can change the right substrate for later features. Do not implement a migration until the spike produces a concrete recommendation.

See [API-SERVER-PARITY-SPIKE.md](API-SERVER-PARITY-SPIKE.md).

## Parallel watch: R2 — upstream native/session authority

Track Hermes upstream work around:

- unified gateway session authority;
- supported local/native attach;
- ordinary prompt admission/turn identity;
- profile-scoped native authorization.

If Hermes lands a supported seam that supersedes the private owner adapter, prefer migration over growing a permanent bridge-specific fork.

This is a watch/research lane, not a reason to block unrelated durable work while the upstream contracts remain open.

See [UPSTREAM-HERMES.md](UPSTREAM-HERMES.md).

## After R1: P1 — interactive server requests

Approvals, clarify, sudo/secrets, vault unlock, and similar server→client requests require a separate security design.

Do not let one unattended model silently authorize privileged requests for another. Any implementation should expose explicit pending-request identity and answer/cancel methods.

Why this comes before remote HTTP MCP: it closes a real capability gap in controlling Hermes safely, while preserving the existing local stdio trust model.

## Later: P2 — Streamable HTTP MCP

This is the intended long-term remote UX after UX1: keep the bridge beside
Hermes so profile credentials remain local, and expose the MCP control surface
through an authenticated remote transport instead of asking users to mirror
Hermes secret trees onto another machine.

Useful for remote MCP clients, but it creates a new trust boundary:

- listener lifecycle;
- authentication;
- origin/network exposure;
- DoS/resource bounds;
- TLS/reverse-proxy guidance.

Stdio remains the default until this boundary is designed and there is a concrete remote-client need.

## Deferred: P3 — cross-platform live IPC

Do not independently duplicate upstream local-authority work while Hermes is actively evolving it. Consume a stable upstream seam if one lands; reopen a bridge-owned IPC design only if upstream direction stalls or exposes a stable contract we should wrap.

## Explicit non-goal: speculative Stage 3 A2A

Do not build broad peer/A2A orchestration merely because Hermes can be controlled through MCP.

Revisit only for a concrete use case that cannot be expressed through the control plane, profiles, or a supported Hermes upstream capability.
