# hermes-zcode-bridge — project contract

## Purpose and boundaries

Standalone MCP stdio adapter for ZCode to drive the installed Hermes API Server
`/v1/runs`. It owns only a small client-side registry and request lifecycle;
Hermes core, Desktop/TUI, gateway code, and the shared worktree are out of
scope for this repository.

Stage 1 scope: durable API runs (`start`, `status`, `wait`, `events`, `stop`,
`steer`, `history`, health probes), explicit idempotency/reconciliation, and
contract tests without LLM calls. Stage 2 adds a thin live TUI WebSocket client
and MCP facade; it does not embed Hermes core or replace Desktop/TUI. A2A/peer
interoperability remains backlog.

Stage 2 live surface (target; local shared attach is not yet production-ready):

- `live_session_open` connects and creates/resumes a durable session;
- `live_prompt` submits one prompt; `live_wait` waits for its start/complete pair;
- `live_events` reads the bounded event buffer; `live_status`/`live_history` are
  recovery reads; `live_steer`/`live_interrupt` are exact-session controls;
- `live_reconnect` explicitly reconnects and replays retained per-session events;
- `live_health` reports connection/auth/replay state without submitting a prompt.

The target live client uses the existing TUI JSON-RPC/WS protocol through a
cooperative local owner attach. The target must not require Dashboard access or
refresh tokens, browser cookies, web tickets, `auth_required=false`, or a reused
`API_SERVER_KEY`. Prefer an OS/process-bound Unix-socket boundary with owner lease,
PID/liveness and profile fencing; loopback HTTP is acceptable only after the same
admission boundary is proven. The current code retains historical token/ticket
compatibility for tests, but it is not the deployment target.

## Source of truth and deployment

- Source: this repository (`/path/to/hermes-control-mcp` on the
  server during local development).
- Hermes API target: configured URL, default `http://127.0.0.1:8642`; the API
  key is read from the process environment or the server-side Hermes `.env`,
  never from tracked files, MCP arguments, URLs, or logs.
- ZCode consumes the long-lived stdio process through its existing MCP config;
  SSH, when used, wraps this process once rather than once per tool call.
- Registry default: XDG state directory, SQLite permissions `0700/0600`.
- Obsidian project card and active plan are the human review surface:
  `~/obsidian-vault/projects/hermes-zcode-bridge/README.md` and
  `~/obsidian-vault/active-tasks/2026-09-13-hermes-zcode-bridge-plan.md`.

## Invariants

- Keep agent/profile, durable session, API run, transport connection, and
  caller request ID as separate identities.
- Store only lane/session mapping and redacted request state; never store raw
  prompts, API keys, bearer headers, or response bodies that are not needed for
  the returned answer.
- One logical request gets one caller request ID and one `Idempotency-Key`.
  An uncertain submit is `unknown`; only an explicit retry with the same key
  may reconcile it. Never blindly resend an uncertain POST with a new key.
- Preserve exact provider/model/session values supplied by the caller; do not
  normalize provider aliases or silently fall back.
- Control operations address an exact `run_id`; no broadcast or title lookup.
- MCP tools are an allowlist. Do not expose shell, CLI, config mutation, raw
  RPC dispatch, or slash commands.
- API errors are structured and secret-redacted. stdout is reserved for MCP
  protocol frames; diagnostics go to stderr.

## Checks

```bash
./scripts/test.sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
python3 -m py_compile src/hermes_zcode_bridge/*.py
```

The test suite is stdlib `unittest` with deterministic fake transports; a
separate non-consuming loopback smoke uses the real `websockets` client/server
handshake and replay path. A live health/models/capabilities probe is also
non-consuming; an LLM turn is deliberately deferred by the project owner until
the bridge is more mature.

## Style and changes

Use Python 3.11+ with stdlib-first code and bounded `mcp`, `httpx`, and
`websockets` dependencies only for protocol serving/client transport. Keep
modules focused; use TDD RED → GREEN → REFACTOR.
Do not add a new daemon, queue, broker, database service, or UI. Behavioral
configuration belongs in a tracked config/CLI argument; secrets belong in the
server `.env`. Public publication, package release, and production deploy need
explicit authorization.
