# Changelog

All notable user-facing changes to this local bridge are documented here.

## [Unreleased]

### Changed

- Stage 2 candidate now supports an explicit `--gateway-owner-lease` mode for
  cooperative attach to an existing Hermes Desktop/TUI session through a
  private Unix socket; Dashboard web-token and `/api/ws` are not reused.
- Owner mode is disabled/unconfigured by default, advertises
  `client.capabilities(server_requests=false)`, preserves stored/runtime
  session identities, and keeps the durable Stage 1 lane as fallback.
- MCP live-tool descriptions now distinguish the stored/durable ID accepted by
  `live_session_open` from the runtime ID used by explicit live read/control
  arguments; lane-based routing is documented as the stable default.
- The ZCode Stage 2 example uses the `hermes_stage2` server name and deployed
  private-owner lease command instead of the legacy durable-only entry.

### Added

- Initial MCP stdio bridge for Hermes Agent durable `/v1/runs`.
- Lane/session registry, request fingerprints, explicit same-key recovery,
  bounded status/wait, SSE events, exact stop/steer, history, and health probes.
- SSH one-process example for ZCode without a new UI or exposed credentials.
- Lease validation, owner identity fencing, UDS attach and a disposable
  existing-session `resume`/`activate`/reconnect smoke. Production enablement
  and consuming LLM smoke remain intentionally out of scope.
