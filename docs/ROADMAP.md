# Roadmap

This document is the implementation-facing roadmap for hermes-zcode-bridge.

The project is intentionally split into small reviewable stages. A roadmap item
is not permission to pull adjacent items into the same PR.

## Design principles

1. **One Hermes authority.** The bridge is a client/control plane, not a second
   runtime.
2. **Durable and live are different products.** Durable API work and shared
   live-session attach have different failure contracts and stay explicit.
3. **Conservative recovery beats false success.** Unknown/ambiguous outcomes
   must not become guessed answers or duplicate mutations.
4. **Profiles are security boundaries.** Profile identity includes config,
   credentials, state, memory and routing; it cannot be ambient decoration.
5. **Prefer upstream seams over permanent forks.** When Hermes is actively
   converging on a generic capability, avoid building a competing private stack.
6. **Public release comes after contract clarity.** Packaging must not freeze
   accidental Stage 1/2 internals.

## Completed milestones

### Stage 1 — durable control plane

Status: complete.

- MCP stdio server;
- /v1/runs submit/status/wait/events;
- exact stop/steer;
- durable session history;
- lane registry;
- request fingerprints and idempotency;
- explicit unknown-outcome reconciliation;
- health/models/capabilities probes.

### Stage 2 — shared live attach

Status: complete for the bounded local-owner architecture.

- private owner attach to the same TUI gateway runtime;
- stored/runtime identity split;
- live prompt/status/history/events/control;
- reconnect and retained-event replay;
- owner lease/PID/profile fencing;
- no Dashboard credential reuse;
- client.capabilities(server_requests=false).

The owner adapter remains an out-of-tree Hermes seam and is not yet a supported
upstream public boundary.

### Stage 2.1 — live correctness hardening

Status: merged and deployed.

- claimed-turn attribution through inflight evidence;
- proof watermark + replay epoch + connection generation;
- conservative reconnect/replay handling;
- failed/interrupted outcome correctness;
- atomic live request_id reservation;
- pre-submit durable boundary for reconciliation;
- older-identical-prompt false reconciliation removed;
- explicit documentation of the same-text upstream limitation.

Merged bridge main: b786e6da83db575f6637897214c1c4442c2d867c.

## Stage 2.2 — public-beta foundation

Stage 2.2 is a family of bounded PRs, not one branch.

### Stage 2.2A — lifecycle recovery

Status: **complete** — PR #5 merged as `999ccff`; 79-test receipt, Pytna
remediation/reread PASS, independent review PASS.

Independent review retained one LOW observability note: two concurrent
conservative waiters may leave the last recovery `error_code` on an already
unknown row. This cannot produce false success, duplicate a mutation or prevent
reconciliation, so it is not a milestone blocker.

Historical contract: [STAGE-2.2-IMPLEMENTATION-BRIEF.md](STAGE-2.2-IMPLEMENTATION-BRIEF.md).

Completed outcomes:


- make conservative live_wait outcomes that instruct the caller to reconcile
  actually transition into a state accepted by live_reconcile;
- handle Hermes' exact transient
  `4007 "session no longer live; retry resume"` race with one bounded
  session.resume retry;
- preserve genuine `4007 "session not found"` as a hard recovery boundary;
- add process-restart/lane recovery coverage without auto-forking conversations.

Not in scope:

- profile routing;
- new MCP methods;
- interactive server requests;
- Streamable HTTP MCP;
- cross-platform owner transport;
- package release.

### Stage 2.2B — first-class multi-profile routing

Status: **NOW**.

Priority: release blocker for public beta.

Exact contract:
[STAGE-2.2B-IMPLEMENTATION-BRIEF.md](STAGE-2.2B-IMPLEMENTATION-BRIEF.md).

Problem today:

- durable API calls target one configured API base, normally the unprefixed
  default-profile listener;
- live session.create accepts a profile, but the bridge registry does not
  durably bind that profile to the lane;
- after bridge restart, session.resume currently does not preserve a named
  profile selection.

Target identity:

~~~text
(profile, lane) -> stored_session_id
~~~

rather than:

~~~text
lane -> stored_session_id
~~~

Required behavior:

- persist profile with lane and live request records;
- default legacy rows safely to profile=default;
- pass profile on live create and resume;
- prevent a named-profile lane from silently falling through to default;
- route durable API calls through `/p/<profile>/...`;
- use the target profile's own API_SERVER_KEY;
- scope idempotency/reconciliation to profile identity;
- keep runtime IDs ephemeral and lane routing stable;
- add two-profile isolation tests.

Open design question:

Whether one bridge process owns a credential resolver for all served profiles or
whether profile-specific bridge instances remain a supported deployment mode.
The default public UX should prefer one client-neutral bridge with explicit
profile routing if it can be done without weakening secret isolation.

### Stage 2.2C — public package hardening

Status: **IN PROGRESS**.

Behavioral base includes Stage 2.2B.1 omitted-profile inference fix
`0d5b8be`.

Accepted public-beta model:

~~~text
durable core = stable / stock supported Hermes
live shared-session attach = experimental / optional
~~~

Priority: current milestone.

Target work:

- client-neutral README/examples;
- non-consuming `doctor` readiness command;
- stable durable vs experimental live capability tiers;
- package metadata suitable for a public Python release;
- MIT license;
- CI on Python 3.11/3.12/3.13;
- wheel + sdist clean-install MCP stdio smoke;
- isolated runner/uvx-style invocation if packaging supports it;
- public registry schema v1 ownership;
- documented one-process-per-state-db contract;
- generic configuration examples without private hostnames or local paths;
- release versioning and changelog discipline;
- explicit compatibility matrix against Hermes versions/seams;
- final distribution/CLI naming before publication.

Primary distribution remains a standalone MCP package. A Hermes plugin may
later act as an installer/discovery companion; it does not own the stdio runtime
in the public beta.

Deployment lessons from the production multi-profile rollout are treated as
release prerequisites rather than a new runtime fix: doctor/compatibility docs
must detect or explain missing named-profile keys, unserved profiles, stale
owner attach and legacy multiplex adapter conflicts. The bridge does not mutate
Hermes topology to repair them.

## Parallel research gates

### R1 — API Server / Agent Sessions parity spike

Hermes now exposes richer session-oriented API endpoints in addition to
/v1/runs. Before freezing the bridge's public durable contract, compare:

- /v1/runs;
- /api/sessions/{id}/chat;
- /api/sessions/{id}/chat/stream;
- TUI owner attach.

Questions include idempotency, detached execution, SSE replay, controls,
approvals, compression/session continuity and profile routing.

Plan: [API-SERVER-PARITY-SPIKE.md](API-SERVER-PARITY-SPIKE.md).

### R2 — upstream unified session authority watch

Track at least:

- NousResearch/hermes-agent PR #106742;
- issue #109891;
- issue #62857.

Do not assume any of these are merged until verified.

If upstream lands a versioned local control endpoint, durable admission identity
or native scoped grant that supersedes our private owner seam, prefer an adapter
migration over growing our fork.

### R3 — ordinary prompt turn/admission identity

Stage 2.1 still cannot perfectly distinguish byte-identical competing writers in
a narrow terminal/reconnect window because ordinary prompt.submit lacks a
general server-issued identity that is echoed through the terminal event.

Re-evaluate whenever upstream changes PromptSubmitResult/message events or lands
the admission model from unified-authority work.

## Important post-2.2 features

These are important to the eventual product but are not all required in the
same milestone.

### P1 — interactive server requests

Examples: approval, clarify, sudo, secret, vault unlock, connection and
read/act bridges.

Required architecture:

~~~text
Hermes server request
        |
        v
bridge pending-request record
        |
        v
MCP caller receives human_action_required
        |
        v
explicit answer/cancel MCP method
~~~

Do not let one LLM silently authorize another LLM's privileged request.

Stage 2.2 may design this contract, but implementation should be a separate
security-reviewed PR.

### P2 — Streamable HTTP MCP

Useful for public remote clients, but it creates a new trust boundary:

- listener lifecycle;
- authentication;
- sessions;
- origin/network exposure;
- DoS/resource bounds;
- TLS/reverse-proxy guidance.

Stdio remains the default until this boundary is designed.

### P3 — multiple Hermes profiles

Promoted into Stage 2.2B because current Hermes treats multiplex profiles as a
first-class topology and public bridge semantics would otherwise be misleading.

### P4 — cross-platform live IPC

Do not independently implement Windows named pipes/macOS/Linux variants while
upstream PR #106742 is actively pursuing the same authority/transport problem.

Re-open this item only if upstream direction stalls or exposes a stable contract
we can consume.

## Explicit non-goal: Stage 3 A2A orchestration

Do not begin a broad A2A/peer-agent architecture merely because the bridge can
control Hermes.

Hermes upstream is itself evolving peer, gateway, profile and durable-admission
semantics. The bridge should remain an MCP control plane unless a concrete
external-client use case requires more.
