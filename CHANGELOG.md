# Changelog

All notable public changes to Hermes MCP Control Plane are documented here.

## [Unreleased]

## [0.2.0b1] - 2026-09-20

First public beta.

### Added

- Durable MCP control plane for Hermes Agent runs: submit, status, wait, events, stop, steer, history, and health.
- Optional shared live-session control for compatible Hermes owner/native attach deployments.
- First-class multi-profile routing across durable API and live session operations.
- Conservative request identity, idempotency, reconciliation, replay, and reconnect handling.
- Non-consuming `hermes-control-mcp doctor` with named-profile probes, JSON output, and optional `--require-live` enforcement.
- Public registry schema v1 with legacy additive migration and fail-closed handling of future unsupported schema versions.
- Python 3.11–3.13 CI plus wheel/sdist build and clean installed-artifact MCP stdio smoke.
- MIT license and public compatibility/upstream documentation.

### Fixed

- Shared live-turn attribution no longer accepts foreign completions merely because they are next in the event stream.
- Reconciliation no longer treats older identical prompts as evidence for a new ambiguous submit.
- Conservative `live_wait` recovery now leaves the request in a state accepted by `live_reconcile` without overwriting terminal outcomes.
- Hermes transient `4007 "session no longer live; retry resume"` receives one bounded exact resume retry; genuine missing sessions remain fail-closed.
- Omitted profile routing now infers existing exact/local identity before defaulting, preventing silent named-profile → default-profile admission drift.
- Profile-scoped replay, controls, reconciliation, error redaction, and API-key isolation were hardened through adversarial regression coverage.

### Public-beta boundaries

- Durable API control is the stable stock-Hermes tier.
- Shared Desktop/TUI live attach is experimental/optional and currently requires a compatible owner/native attach seam.
- One bridge process should own one state DB.
- Interactive approval/clarify requests and remote Streamable HTTP MCP are not implemented in this beta.
