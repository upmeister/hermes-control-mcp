# hermes-zcode-bridge

An experimental MCP control plane for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

The project started as a ZCode integration, but the bridge itself is an MCP stdio server and is intentionally moving toward a client-neutral public package. It exposes two complementary control planes:

- **Durable runs** over the Hermes API Server for idempotent, detachable work.
- **Live shared sessions** for attaching to the same Hermes TUI/Desktop runtime without creating a second session authority.

> **Project status:** Stage 2.2A is merged after independent review. The bridge now has the Stage 2.1 shared-turn safety model plus actionable wait→reconcile recovery and a bounded retry for Hermes' transient resume race. Stage 2.2B is the current implementation milestone: make Hermes profile identity first-class across lanes, durable API routing and live resume/control. The project is not public-release-ready yet: live owner attach still depends on an out-of-tree Hermes seam, and packaging/release hardening follows multi-profile correctness.

## Why this exists

Hermes already exposes several useful programmatic surfaces, but they have different lifecycle semantics.

The API Server is a good fit for durable jobs: start a run, disconnect, reconnect later, inspect status, and rely on idempotency. The TUI gateway is a better fit when an external coding agent needs to participate in the **same live conversation** a human sees in Hermes Desktop/TUI.

This bridge keeps those two modes explicit instead of pretending they are interchangeable.

~~~text
MCP client
   |
   | stdio (locally or over SSH)
   v
hermes-zcode-bridge
   |
   +-- durable plane --> Hermes API Server --> /v1/runs, history, status, control
   |
   +-- live plane ----> private owner attach --> existing TUI gateway runtime
                                             --> same live session as Desktop/TUI
~~~

The bridge is deliberately thin: it does not embed Hermes core, create a second agent runtime, expose raw gateway dispatch, or silently retry ambiguous mutations.

## Current capabilities

### Durable plane

- idempotent run submission with caller request IDs;
- explicit lane-to-session continuity;
- bounded status/wait/event collection;
- exact-run stop and steer;
- durable session history;
- structured recovery after uncertain transport outcomes;
- Hermes health/models/capabilities probes.

Primary MCP tools:

~~~text
run_start
run_status
run_wait
run_events
run_stop
run_steer
session_history
bridge_health
~~~

### Live plane

- create or resume a Hermes live session;
- attach to an existing owner runtime rather than spawning a competing one;
- submit prompts without blind retry;
- bounded completion waiting with shared-session attribution checks;
- event replay and reconnect handling;
- live status/history;
- steer and interrupt;
- durable-history reconciliation after ambiguous submit/stream outcomes.

Primary MCP tools:

~~~text
live_session_open
live_prompt
live_wait
live_events
live_status
live_history
live_steer
live_interrupt
live_reconcile
live_reconnect
live_health
~~~

## Identity model

Several identifiers that look similar are intentionally kept separate:

- **profile** — Hermes configuration/state/memory boundary;
- **lane** — bridge-side stable routing name (unique per profile, not globally);
- **stored session ID** — durable Hermes conversation identity;
- **runtime session ID** — ephemeral ID owned by one live TUI gateway process;
- **run ID** — one durable API execution;
- **request ID** — caller-side logical request identity;
- **connection generation / replay epoch / event sequence** — live transport evidence.

For live calls, prefer the bridge lane after opening a session. Runtime session IDs are ephemeral and must not be treated as durable handles.

## Profile routing

Hermes profile identity is a first-class routing and security boundary. The public invariant is:

~~~text
(profile, lane) -> stored_session_id
~~~

- An omitted `profile` first inherits an exact locally known request/idempotency/session identity or one unambiguous existing lane profile. Only a genuinely new, unbound admission defaults to `default`. Lane-only ambiguity fails closed with `lane_profile_ambiguous`; an explicitly supplied profile that disagrees with stored identity is a conflict, never a reroute.
- The same lane name may legally exist in two profiles; live runtime routing keys on `(profile, lane)`.
- Live create/resume/activate, prompt submission, status/history/events, steer and interrupt carry the resolved profile; it survives restart, reconnect and reconcile (reconcile uses the request's stored profile). The transient `4007` resume retry repeats the identical profile-scoped params.
- Durable API calls for a named profile route through Hermes `/p/<profile>/...` and resolve that profile's own `API_SERVER_KEY` from `<profiles_root>/<profile>/.env` (`--profiles-root`, default `$HERMES_HOME/profiles`). A missing named-profile key fails closed; the default profile key is never inherited for named profiles.
- Profile ids mirror the upstream Hermes syntax `^[a-z0-9][a-z0-9_-]{0,63}$` and are validated before any filesystem or URL use.
- The registry stores profile names as routing metadata — never keys or other secret material. Existing databases migrate additively: legacy rows read as `default`.

## Safety and recovery rules

The bridge is conservative by design.

- A mutating request with an uncertain acknowledgement is **not** silently resubmitted.
- One logical durable request keeps one idempotency key.
- Live completions are not accepted solely because they are the next event in a buffer.
- Reconnects invalidate live ownership proofs.
- Replay gaps and epoch changes fail closed into recovery rather than guessing.
- A conservative live result that advises reconciliation leaves the request stored as reconcilable (`unknown`); ordinary bounded waits with a still-running turn keep the request retriable.
- Hermes' transient `4007 "session no longer live; retry resume"` receives exactly one bounded `session.resume` retry during stored-session attach; a genuine `4007 "session not found"` never retries and never auto-creates a session.
- The bridge registry stores IDs, hashes, cursors, status and routing metadata — not raw prompts or credentials.
- MCP tools are an allowlist. Raw shell, arbitrary gateway RPC, slash commands and configuration mutation are not exposed.

Stage 2.1 still has one upstream-limited attribution edge: the current TUI prompt protocol does not provide a general server-issued turn/admission identity for ordinary prompt submission. Byte-identical competing prompts can therefore remain fundamentally indistinguishable in a narrow terminal/reconnect window. See [Upstream research](docs/UPSTREAM-HERMES.md).

## Live owner attach

The preferred live deployment uses a private, same-user local owner boundary:

~~~text
Hermes owner process
  |
  +-- existing TUI gateway/session registry
  |
  +-- private Unix-domain socket
        |
        +-- owner lease + PID/process/profile fencing
              |
              +-- bridge WebSocket client
~~~

The bridge does **not** reuse Dashboard cookies, browser refresh tokens, public WebSocket credentials, or the API Server key for this path.

The current owner adapter is an out-of-tree Hermes integration and remains opt-in/default-disabled. The long-term goal is to converge on a supported upstream machine/native attach seam rather than permanently maintaining a private transport fork. See [ADR-0001](docs/adr/0001-local-owner-attach.md) and [Upstream research](docs/UPSTREAM-HERMES.md).

## Running from source

Python 3.11+ is required.

~~~bash
git clone https://github.com/upmeister/hermes-zcode-bridge.git
cd hermes-zcode-bridge

python -m venv .venv
. .venv/bin/activate
pip install -e .
~~~

The package exposes:

~~~bash
hermes-zcode-bridge --help
~~~

A typical stdio MCP client launches the bridge as a long-lived process. If Hermes runs on another machine, SSH can wrap the process once for the lifetime of the MCP connection:

~~~json
{
  "mcpServers": {
    "hermes": {
      "command": "ssh",
      "args": [
        "-T",
        "hermes-host",
        "/path/to/hermes-zcode-bridge/.venv/bin/hermes-zcode-bridge",
        "--state-db",
        "~/.local/state/hermes-zcode-bridge/bridge.db"
      ]
    }
  }
}
~~~

Secrets should stay on the Hermes host. Do not place API keys, Dashboard credentials, owner identity material, or bearer tokens in client configuration.

Live owner attach additionally requires a compatible owner-adapter lease:

~~~bash
hermes-zcode-bridge \
  --gateway-owner-lease "$HERMES_HOME/runtime/owner_adapter/owner_adapter.json"
~~~

This is currently an integration/development setup, not yet the recommended public installation path.

## Tests

~~~bash
./scripts/test.sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
python3 -m py_compile src/hermes_zcode_bridge/*.py
~~~

The project uses deterministic fake transports for most protocol tests plus real WebSocket/UDS smoke coverage where transport behavior matters.

## Roadmap

### Completed

- **Stage 1** — durable MCP control plane over the Hermes Runs API.
- **Stage 2** — private owner attach to an existing live Hermes TUI runtime.
- **Stage 2.1** — shared-turn attribution hardening, replay conservatism, boundary-aware reconciliation, and atomic live request reservation.
- **Stage 2.2A** — actionable conservative wait recovery plus one bounded retry for Hermes' exact transient `4007 "session no longer live; retry resume"` race. Merged as `999ccff`; independent review: PASS.
- **Stage 2.2B** — first-class multi-profile routing: profile-aware lane/request registry with safe legacy migration, profile-scoped live create/resume/control across restart and reconnect, durable `/p/<profile>/...` routing with per-profile credentials, and fail-closed ambiguity/conflict handling.

### Next: Stage 2.2 — public-beta foundation

Stage 2.2 is intentionally split into bounded PRs:

1. **Lifecycle recovery — complete (Stage 2.2A).** Conservative wait results persist reconcilable state; bounded single retry for Hermes' transient resume race; genuine missing sessions stay fail-closed.
2. **First-class multi-profile routing — complete (Stage 2.2B).** Profile is part of durable lane identity, survives restart/resume, scopes every live TUI call, and routes durable API work through Hermes `/p/<profile>/...` with the profile's own API key.
3. **Public packaging/hardening — next (Stage 2.2C).** Client-neutral naming/docs, clean-install smoke, CI/release metadata, registry schema ownership and generic examples.

Research gates run alongside implementation:

- compare `/v1/runs` with Hermes' newer Agent Sessions API;
- track upstream unified-session work and native/machine-client authorization;
- avoid building a competing cross-platform IPC layer while upstream is actively converging on one.

Detailed plan: [ROADMAP.md](docs/ROADMAP.md).

## Hermes upstream watch

Hermes is moving quickly in exactly the areas this bridge depends on. As of the 2026-09-20 research snapshot:

- the stable release is v0.21.3 / v2026.9.14;
- the API Server supports a richer Agent Sessions API, including session chat and SSE streaming;
- multiplexed profiles are served through `/p/<profile>/...` with profile-scoped authentication; current TUI `SessionParams` also carries `profile`, so create-only profile routing is insufficient;
- upstream PR #106742 proposes one gateway-owned session authority across local surfaces, including durable admission identity and multi-profile ownership;
- issue #109891 discusses making that gateway a first-class Desktop backend;
- issue #62857 proposes scoped native WebSocket grants.

These are inputs to our architecture, not dependencies we pretend are already merged. See [UPSTREAM-HERMES.md](docs/UPSTREAM-HERMES.md).

## Distribution direction

The likely public distribution model is:

1. **Primary:** standalone Python package, runnable directly or through `uvx`/an equivalent isolated package runner.
2. **Optional:** a Hermes plugin companion that improves installation, discovery, configuration or owner-seam integration.
3. **Upstream:** contribute the smallest generally useful Hermes core seams instead of permanently carrying a private fork.

Hermes plugins can be distributed through Git/Python entry points and support an external-runtime/sidecar pattern, so a plugin wrapper is technically possible. However, the bridge is an **external MCP server whose lifetime is owned by the MCP client**; forcing that runtime inside Hermes would blur an otherwise useful process boundary.

The full rationale and an upstreaming proposal are documented in [DISTRIBUTION-AND-UPSTREAMING.md](docs/DISTRIBUTION-AND-UPSTREAMING.md).

## Should this eventually live in Hermes itself?

Possibly — but not as the first move.

The strongest near-term upstream contribution is the generic Hermes-side contract the bridge needs: supported machine/native attach, stable session/admission identity, profile-scoped routing, and capability-limited authorization. Those benefit Hermes independently of this MCP client.

After the bridge has a public beta and the upstream session-authority direction settles, there is a reasonable case for proposing either:

- a bundled `hermes mcp` integration;
- an official Hermes plugin/package;
- or moving the bridge itself into the Hermes repository if maintainers want MCP as a supported external control-plane surface.

Until then, keeping the bridge standalone lets it iterate quickly without coupling its release cadence to Hermes core.

## Documentation

- [Stage 2.2 roadmap](docs/ROADMAP.md)
- [Current Stage 2.2B coding contract](docs/STAGE-2.2B-IMPLEMENTATION-BRIEF.md)
- [Historical Stage 2.2A implementation brief](docs/STAGE-2.2-IMPLEMENTATION-BRIEF.md)
- [Hermes upstream research](docs/UPSTREAM-HERMES.md)
- [API Server / Agent Sessions parity spike](docs/API-SERVER-PARITY-SPIKE.md)
- [Distribution and upstreaming strategy](docs/DISTRIBUTION-AND-UPSTREAMING.md)
- [ADR-0001: private owner attach](docs/adr/0001-local-owner-attach.md)

## Development contract

Read [AGENTS.md](AGENTS.md) before changing behavior. It defines the current scope, safety invariants and required checks.

## License and public release

A public release still needs an explicit license decision and release metadata. Until that is done, treat this repository as pre-public engineering work rather than a finished third-party distribution.
