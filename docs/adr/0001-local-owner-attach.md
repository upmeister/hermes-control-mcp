# ADR-0001: Private owner adapter for Stage 2 live attach

- **Status:** Accepted for Stage 2 merge. The adapter remains opt-in/default-disabled in code; later production enablement/deployment is an operational choice, not part of this ADR's original merge gate.
- **Scope:** `hermes-control-mcp` Stage 2 live attach.
- **Decision:** Use an opt-in, process-bound Unix-domain-socket owner adapter in the same Hermes web server/event loop. The bridge consumes its private lease and never reuses Dashboard `/api/ws` authentication.

## Context

The old Stage 2 client already had live transport, replay and reconnect logic, but
its attach boundary depended on a Dashboard/web token. The current Hermes runtime
already owns the session registry and `FanoutTransport`; creating a second runtime
or copying a web credential would create a second authority and bypass the wrong
trust boundary.

The owner adapter is therefore a narrow admission layer, not a second session
implementation. It delegates an admitted WebSocket to the existing
`tui_gateway.ws.handle_ws` path.

## Data flow

```text
Hermes owner process
  ├─ existing TCP/dashboard listener (/api/ws, unchanged)
  └─ same uvicorn Server/event loop
       └─ private UDS: runtime/owner_adapter/owner_adapter.sock
            └─ /api/owner/ws
                 └─ existing tui_gateway.ws.handle_ws / server.dispatch

bridge --gateway-owner-lease owner_adapter.json
  └─ validate lease + private UDS
       └─ websockets.asyncio.client.unix_connect(socket, uri=identity_query)
```

The lease is identity metadata, not a secret. It contains the protocol version,
`runtime_id`, owner PID, process-start marker, profile home, socket/lease paths,
route, transport and bound server metadata. The bridge query contains only
`runtime_id`, `pid`, `process_start` and `profile_home`.

## Trust boundary and fencing

Owner admission is accepted only when all of the following hold:

1. The ASGI `server` scope is the exact advertised UDS path.
2. The socket is a same-UID private Unix socket.
3. The lease is readable, private, unchanged and matches the owner identity.
4. The owner PID is alive and its process-start marker is unchanged.
5. Every identity query field matches the current lease.
6. The owner adapter's runtime lock prevents concurrent stale-path cleanup/bind races.

Bridge-side admission independently rejects non-private lease/socket paths,
symlinked lease paths, mismatched paths, invalid schema, broad permissions,
non-Unix sockets and invalid endpoints. It uses `unix_connect`; an injected TCP
connector is not used in owner mode.

The owner gate is disabled by default (`dashboard.owner_adapter.enabled`). The
owner adapter runs inside the existing Hermes process; no second runtime, daemon,
broker, public listener or Dashboard auth bypass is introduced.

## Session state machine

```text
lease absent/invalid ──> owner_attach_failed / durable Stage 1 fallback
        │
        ▼
private UDS + exact identity
        │ gateway.ready
        ▼
client.capabilities(server_requests=false)
        │
        ▼
session.resume(stored_session_id)
        │ existing live key found → FanoutTransport rebind
        ▼
session.activate(runtime_session_id)
        │
        ├─ disconnect → one viewer detaches; owner/session survives
        └─ reconnect → new WebSocket generation; explicit resume/activate
```

The bridge keeps stored and runtime session IDs separate. It does not silently
resubmit prompt/steer/interrupt after an uncertain acknowledgement. The existing
bridge replay ring and cursor/epoch logic remains the recovery layer; durable
history/status remains the authority when live replay is unavailable.

## Failure matrix

| Failure | Required result |
|---|---|
| Missing/corrupt/unreadable lease | Reject local attach; preserve owner state; keep durable lane available |
| Wrong runtime/PID/start/profile identity | Reject handshake; do not mutate lease or session |
| TCP attempt to owner route | Reject; UDS scope is mandatory |
| Concurrent owner startup | Non-blocking runtime lock rejects the loser before stale cleanup |
| Owner disconnect | Bridge reports closed/degraded; no prompt retry |
| Replay epoch/cursor gap | Existing bridge replay/degraded recovery policy; no generation mixing |
| Unsupported server→client request | Capability false; bridge is not interactive authority |
| Owner shutdown | Lease/socket cleanup; next attach must rediscover exact owner |

## Evidence

The candidate was tested in isolated worktrees, without restarting the production
Hermes runtime:

- owner focused tests: `4 passed`;
- shared-session tests: `2 passed`;
- owner compile: exit `0`;
- `OWNER_UDS_E2E=PASS`: lease publication, UDS handshake, `gateway.ready`, ping,
  wrong identity rejection, TCP bypass rejection and lifecycle cleanup;
- bridge full suite: `47 tests passed`;
- bridge compile and `ResourceWarning` unittest discovery: exit `0`;
- `BRIDGE_OWNER_UDS_E2E=PASS`: real bridge client through UDS, capability false,
  ping and no injected TCP path;
- `EXISTING_SESSION_ATTACH=PASS`: disposable seed session, exact stored/runtime
  identity, `session.resume`, `session.activate`, original Desktop-like client
  remains usable, and a bridge reconnect performs the same attach again.

The existing bridge unit tests cover replay gap ordering, epoch changes, bounded
buffers and unknown mutation outcomes. The disposable owner fixture deliberately
sends no consuming prompt, so a live event-gap/replay trace through the owner is
not claimed here. Capability negotiation is exercised by the bridge test and real
owner attach; an actual approval/clarify request is intentionally not generated.

## Rejected alternatives

- Dashboard `/api/ws` plus copied cookie/token: wrong trust boundary and forbidden
  credential reuse.
- `auth_required=false`: removes admission rather than proving ownership.
- Loopback-only TCP owner route: insufficient process/session fencing for this
  local IPC use case.
- Second Hermes runtime or broker: duplicates authority and adds failure points.
- Blind reuse of `internal_ws_credential`: secret transfer from an unrelated web
  surface.

## Release boundary

This ADR records the accepted Stage 2 implementation and its release boundary.
No production config was enabled, no shared service was restarted, and no
consuming LLM turn was sent. The exact-current independent security review gave
GO for the reviewed owner/bridge scope. The full Hermes suite did not finish
within the 420-second runner limit, so that remains a documented release
limitation; owner enablement and any consuming semantic smoke are still
separate operational gates.


## Post-acceptance status note — 2026-09-20

The release-boundary paragraph above records the conditions under which ADR-0001
was originally accepted; it is historical evidence, not a statement that the
adapter has never been enabled since.

After acceptance, Stage 2/2.1 was deployed and exercised in the project's
production environment. The architectural decision remains unchanged: live
attach uses the same owner runtime through the private local boundary and does
not reuse Dashboard credentials.

For current project status, roadmap and upstream migration strategy, see:

- `../../README.md`
- `../ROADMAP.md`
- `../UPSTREAM-HERMES.md`

The owner adapter is still an out-of-tree Hermes seam. Public release must not
describe it as a stock Hermes endpoint until an equivalent supported upstream
contract exists.
