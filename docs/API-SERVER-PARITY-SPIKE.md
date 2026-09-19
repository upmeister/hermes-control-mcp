# API Server / Agent Sessions parity spike

## Purpose

Before the bridge publishes a stable public durable/session contract, determine
which Hermes API surface should be authoritative for each use case.

This is a research spike first. Do not rewrite Stage 1 merely because a newer
endpoint exists.

## Surfaces under comparison

1. `/v1/runs`
2. `/api/sessions/{id}/chat`
3. `/api/sessions/{id}/chat/stream`
4. TUI gateway owner attach

## Core question

Can the newer Agent Sessions API simplify or replace any bridge-owned durable
session logic **without losing** the safety properties already established by
the Runs API and without pretending it is equivalent to a shared live runtime?

## Required comparison dimensions

| Capability | Runs API | Agent Sessions API | TUI owner attach |
|---|---|---|---|
| detached execution | verify | verify | live-process semantics |
| caller idempotency | known strong contract | verify | bridge request registry |
| durable session continuity | yes | expected; verify | stored + runtime split |
| SSE/token/tool progress | yes | yes; verify exact schema | WS events |
| reconnect/replay after client loss | verify exact retention | verify | retained event replay |
| exact stop/cancel | yes | verify | interrupt |
| steer/correction | yes | verify | steer |
| approvals/clarify | verify current runs support | verify | server→client requests |
| compression lineage continuity | verify | verify | TUI semantics |
| profile /p/<profile>/ routing | yes on current upstream; verify | yes; verify | profile params |
| same Desktop/TUI runtime | no | no unless proven otherwise | yes |
| unknown-submit recovery | idempotency/status | verify | boundary history/replay |

Do not fill unknown cells from assumptions.

## Test matrix

Run against a disposable Hermes profile/runtime.

### A. Session creation and continuity

- create an API session;
- send one turn;
- read messages;
- send a second turn after client reconnect;
- verify stable session ID/lineage behavior;
- repeat with explicit model/provider overrides;
- repeat after compression if available.

### B. Streaming semantics

For `chat/stream` capture the exact event sequence for:

- plain text response;
- tool call;
- tool failure;
- model/provider failure;
- cancellation;
- interim assistant commentary.

Record whether event IDs/cursors permit replay or whether SSE is only a live
observation channel.

### C. Ambiguous transport outcome

Inject/force connection loss around submit acknowledgement.

Determine:

- whether there is a caller idempotency key;
- whether the request can be queried by stable execution identity;
- whether retry can duplicate a turn;
- which durable evidence proves the prompt was admitted.

### D. Controls

Probe:

- cancel/stop;
- steer/correction if exposed;
- approvals/clarify;
- status while running;
- result after client disconnect.

### E. Multi-profile

Create default + named profile with distinct state and API keys.

Verify:

- unprefixed request uses default;
- `/p/<profile>/...` uses named state;
- default key is rejected on named prefix;
- named key is rejected/isolated appropriately;
- session IDs and run IDs do not cross profile boundaries.

### F. Relation to live TUI runtime

With Desktop/TUI holding an existing live session:

- query Agent Sessions REST for the stored conversation;
- send an Agent Sessions API turn;
- observe whether it joins the same in-memory runtime, creates independent
  execution, or merely writes the same durable state;
- do not infer shared runtime from shared state.db visibility.

This is the decisive boundary for keeping Stage 2.

## Deliverable

Commit a dated research receipt under `docs/research/` or update
`UPSTREAM-HERMES.md` with:

- Hermes exact SHA/tag;
- config/topology used;
- request/response/event schemas;
- capability matrix with proven yes/no/unknown;
- failure-injection results;
- recommendation for each MCP method family.

## Decision outcomes

Possible valid conclusions:

### Keep Runs API as durable authority

If Runs remains the strongest idempotent detached-job contract, retain Stage 1
and use Agent Sessions only for selected session utilities.

### Move session-oriented durable calls to Agent Sessions

If the new API provides equal idempotency/recovery and cleaner native session
semantics, plan a versioned migration rather than silent behavior changes.

### Hybrid

Likely outcome: Runs for detached jobs, Agent Sessions for explicit durable
conversation operations, owner attach for exact live Desktop/TUI participation.

Any migration must preserve caller-visible identities and recovery guarantees.
