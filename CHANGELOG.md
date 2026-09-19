# Changelog

All notable user-facing changes to this local bridge are documented here.

## [Unreleased]

### Fixed

- Live completion attribution: in a shared attached session, `live_wait` no
  longer accepts the first `message.complete` after a sequence cursor as the
  local answer. A submit acknowledged `streaming` proves ownership of the
  running turn via the gateway inflight snapshot (`session.activate`, SHA-256
  of the stripped prompt text) and a post-proof watermark; a foreign turn's
  completion while the claimed turn is still running is skipped, and cases
  where ownership cannot be proven return conservative `ambiguous_turn` /
  `completion_not_observed` states instead of another client's answer.
- Unknown-submit reconciliation is boundary-aware: `live_prompt` captures a
  redacted pre-submit durable boundary (highest user `row_id`, row count) and
  `live_reconcile` matches only post-boundary user rows. An older identical
  prompt can no longer reconcile an ambiguous submit; multiple indistinguishable
  post-boundary matches stay conservative (`ambiguous_history_match`); records
  without boundary metadata never reconcile. Reconciliation now reads the
  source-verified `text` field of gateway history rows (the previous code read
  a `content` field the real gateway never returns).

### Changed

- `live_wait` on an already-terminal request replays the stored outcome instead
  of re-waiting for a later (possibly foreign) completion.
- Completion acceptance matches the deployed Hermes terminal ordering: the
  gateway clears the inflight snapshot before emitting `message.complete`, so a
  candidate is skipped only while the inflight snapshot still matches the
  claimed prompt, and accepted otherwise.
- Any reconnect (even within the same replay epoch) invalidates the ownership
  proof: `live_wait` re-proves the claim via a fresh inflight snapshot and
  persists the re-proven cursor, or returns `completion_not_observed`.
- Live `request_id` reservation is an atomic SQLite INSERT: concurrent calls
  with the same request_id can never submit two gateway mutations.
- Registry gains additive, redacted live-request columns (`attribution`,
  `proof_seq`, `proof_epoch`, `inflight_sha256`, `boundary_row_id`,
  `boundary_count`); existing databases upgrade in place and legacy rows
  degrade conservatively.

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
