# hermes-zcode-bridge — project contract

## Purpose and boundaries

Standalone MCP control plane for Hermes Agent.

The repository currently exposes two bounded surfaces:

- **Durable plane** — Hermes API Server runs, status, events, exact-run control,
  history and explicit idempotency/reconciliation.
- **Live plane** — cooperative attach to an existing Hermes TUI/Desktop runtime
  through a private owner boundary, with replay/reconnect and conservative
  shared-turn attribution.

The project started as a ZCode bridge, but new work should keep the MCP surface
client-neutral unless a ZCode-specific compatibility requirement is explicitly
called out.

Hermes core, Desktop/TUI, gateway internals and public network listeners remain
outside this repository. The bridge may depend on narrowly-scoped Hermes seams,
but it must not become a second Hermes runtime, broker, queue, shell proxy or
general raw-RPC tunnel.

A2A/peer orchestration is not the current roadmap. Stage 2.2 is a public-beta
foundation phase: lifecycle recovery, multi-profile correctness, packaging and
upstream/API research.

## Current milestone state

- Stage 1: complete — durable /v1/runs control plane.
- Stage 2: complete — private owner attach to one existing live runtime.
- Stage 2.1: complete in merged code — live attribution/replay/reconciliation
  hardening and atomic live request reservation.
- Next coding PR: **Stage 2.2A lifecycle recovery**, defined exactly in
  `docs/STAGE-2.2-IMPLEMENTATION-BRIEF.md`.

Do not silently fold Stage 2.2B multi-profile work, Streamable HTTP MCP,
interactive server requests, plugin packaging or cross-platform IPC into the
2.2A PR. Those are separate review boundaries.

## Source of truth

The GitHub repository is the source of truth for code and implementation-facing
architecture:

- `README.md` — public-facing architecture/status;
- `docs/ROADMAP.md` — milestone plan;
- `docs/STAGE-2.2-IMPLEMENTATION-BRIEF.md` — exact next coding contract;
- `docs/UPSTREAM-HERMES.md` — dated upstream research snapshot;
- `docs/API-SERVER-PARITY-SPIKE.md` — research protocol;
- `docs/DISTRIBUTION-AND-UPSTREAMING.md` — packaging/upstream strategy;
- `docs/adr/` — accepted architectural decisions.

External notes/vault receipts may record review history, but they do not override
the exact repository contract.

## Durable-plane contract

Default API target is `http://127.0.0.1:8642`.

- API keys are resolved from process/server-side environment, never stored in
  tracked files, MCP arguments, URLs or logs.
- One logical request gets one caller `request_id` and one
  `Idempotency-Key`.
- An uncertain mutation outcome is never retried with a new key.
- Exact provider/model/session values supplied by callers are preserved.
- Control operations address exact run identity.
- Server status/history is recovery authority; event streams are observation,
  not a replacement for reconciliation.

Stage 2.2B will make profile routing first-class. Until then, do not pretend an
unprefixed API URL dynamically targets named Hermes profiles.

## Live-plane contract

The preferred live deployment uses the existing Hermes TUI JSON-RPC protocol
through a private local owner adapter.

The live client must not depend on Dashboard cookies, browser refresh tokens,
public WebSocket credentials, `auth_required=false`, copied internal
credentials or reuse of `API_SERVER_KEY`.

The owner adapter is currently an out-of-tree Hermes seam. Keep that distinction
explicit in docs and tests; do not describe it as merged upstream behavior.

Live MCP surface:

- `live_session_open` — create/resume and bind a lane;
- `live_prompt` — submit one prompt, never blind-retry an uncertain mutation;
- `live_wait` — accept only attributable terminal outcomes;
- `live_events` — bounded event reads;
- `live_status` / `live_history` — recovery reads;
- `live_steer` / `live_interrupt` — exact live controls;
- `live_reconcile` — durable-history evidence after unknown submit/outcome;
- `live_reconnect` — reconnect + bounded replay;
- `live_health` — non-consuming transport health.

After `gateway.ready`, the bridge advertises
`client.capabilities(server_requests=false)`. Do not flip this to true without
a separate security design for approvals, clarify, sudo/secrets and other
server→client requests.

## Identity invariants

Keep these identities separate:

- Hermes profile;
- bridge lane;
- stored/durable session ID;
- runtime/live session ID;
- API run ID;
- caller request ID;
- idempotency key;
- live connection generation;
- replay epoch;
- event sequence cursor.

A runtime session ID is process-local and ephemeral. A lane is the preferred
bridge handle after open.

For Stage 2.2B, profile must become part of the durable lane binding; legacy
registry rows may default to `default`, but named-profile identity must never
be inferred from ambient process state.

## Recovery invariants

- Never return a foreign completion just because it is next in the local ring.
- A reconnect invalidates the previous ownership proof.
- Replay gaps/truncation/epoch changes degrade conservatively.
- Failed/interrupted turns return no answer payload.
- Byte-identical competing prompts remain an upstream protocol limitation until
  ordinary prompt submission exposes a general server-issued turn/admission ID.
- A conservative result that instructs the caller to reconcile must actually
  leave the request eligible for `live_reconcile`.
- Hermes transient
  `4007 "session no longer live; retry resume"` may receive only the bounded,
  exact `session.resume` retry defined in the Stage 2.2A brief.
- Genuine `4007 "session not found"` must not auto-create or fork a session.

## Registry and secret boundary

The bridge registry may store only routing/identity and redacted recovery state:
IDs, hashes, cursors, status, profile/lane metadata and timestamps.

Do not persist raw prompts, API keys, bearer headers, owner credentials or
arbitrary response bodies.

Registry files and parent state directories must retain private permissions.

## MCP allowlist

Do not expose:

- shell execution;
- arbitrary CLI execution;
- raw gateway method dispatch;
- slash commands;
- Hermes config mutation;
- credential mutation;
- unrestricted filesystem/process control.

A new capability requires an explicit MCP method with its own validation and
failure contract.

## Stage 2.2A exact scope

The next behavioral PR must follow
`docs/STAGE-2.2-IMPLEMENTATION-BRIEF.md`.

In summary, it contains only:

1. make conservative `live_wait` recovery outcomes actually reconcilable;
2. implement one bounded retry for the exact transient Hermes 4007
   "session no longer live; retry resume" race;
3. preserve genuine not-found behavior;
4. add focused regression tests and update user-facing behavior docs.

It does **not** add multi-profile routing yet.

## Upstream-awareness rules

Hermes upstream is changing rapidly. Before coding against a private field or
wire assumption:

1. inspect the exact deployed/reviewed Hermes revision when behavior matters;
2. compare against current upstream main;
3. distinguish stable release behavior from open PRs/issues;
4. do not present open PR #106742 or issue proposals as merged product behavior.

Current research snapshot: `docs/UPSTREAM-HERMES.md`.

## Distribution direction

Do not redesign runtime ownership merely to fit Hermes' plugin system.

Primary public distribution is expected to remain a standalone Python package /
isolated runner because the MCP client owns bridge process lifetime. A Hermes
plugin may later be a companion installer/discovery/configuration layer.

Do not add plugin manifests, PyPI publication, `uvx` claims, release workflows
or license declarations without an explicit release-scoped PR.

## Checks

Run at minimum:

~~~bash
./scripts/test.sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
python3 -m py_compile src/hermes_zcode_bridge/*.py
git diff --check
~~~

For a behavioral fix, add a regression that is demonstrably red on the prior
head and green on the candidate whenever practical.

For owner/live protocol changes, use deterministic fake transports plus a real
WebSocket/UDS smoke when the change depends on transport behavior.

Do not claim the full Hermes upstream suite unless it was actually run to
completion on the relevant Hermes candidate.

## Style and change discipline

Use Python 3.11+ and stdlib-first code with the existing bounded dependencies.

Prefer small PRs with one architectural purpose. Keep stdout exclusively for MCP
protocol frames and diagnostics on stderr.

No production deploy, service restart, public publication, owner-gate change or
Hermes core mutation is implied by merging a bridge PR. Those are separate
operational decisions.
