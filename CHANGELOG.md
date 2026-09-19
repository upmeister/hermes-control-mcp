# Changelog

All notable user-facing changes to this local bridge are documented here.

## [Unreleased]

### Added

- First-class multi-profile routing (Stage 2.2B): Hermes profile identity is
  now a first-class bridge routing and security boundary. Lane bindings are
  profile-aware (`(profile, lane) -> stored_session_id`), the same lane name
  may legally exist in two profiles, and lane-only lookups infer the single
  bound profile or fail closed with `lane_profile_ambiguous`. Named-profile
  live sessions carry `profile` on create/resume/activate, prompt submission,
  status/history/events, steer and interrupt, across restart, reconnect and
  reconcile (reconcile uses the request's stored profile; it accepts no
  profile argument). The transient `4007` resume retry repeats the identical
  profile-scoped params. Durable API calls for a named profile route through
  Hermes `/p/<profile>/...` and resolve that profile's own `API_SERVER_KEY`
  from `<profiles_root>/<profile>/.env` (`--profiles-root`, default
  `$HERMES_HOME/profiles`); a missing named key fails closed and the default
  key is never inherited. Existing databases migrate additively: legacy rows
  read as the `default` profile, legacy lanes become `default` bindings, and
  default-profile lane dual-writes keep the legacy table as a rollback path.
  MCP tools accept an optional `profile` argument with omission/inference/
  ambiguity semantics documented in their descriptions; results include the
  canonical `profile` where it identifies the target. New structured errors:
  `invalid_profile`, `lane_profile_ambiguous`, `lane_profile_conflict`,
  `request_profile_conflict`, `profile_key_unavailable`,
  `profile_route_config_error`. Reconnect replay (`session.events.since`)
  addresses each buffered session under its remembered profile. API error
  redaction covers every key the client knows (the default key and any
  resolved named-profile key), not only the key used by the current request.
  A runtime-addressed live call whose supplied profile disagrees with the
  stored binding fails closed with `request_profile_conflict` instead of
  routing through the wrong profile.

### Fixed

- Lifecycle recovery (Stage 2.2A): a conservative `live_wait` result that
  hands a request to durable recovery (`completion_not_observed`, and
  `ambiguous_turn` when ownership cannot be proven and reconciliation is
  advised) now persists `status=unknown` with the conservative error code
  before returning, so its own recovery advice is actionable —
  `live_reconcile` accepts only rows stored as `unknown`. Ordinary bounded
  `wait_timeout` with a still-running turn and the retriable
  turn-state-unavailable `ambiguous_turn` deliberately keep the request
  retriable. The unproven/queued wait result is now reported as
  `unknown`/`ambiguous_turn` (previously the stale `queued`/`streaming`
  status) to match the persisted state. The recovery write is a
  terminal-preserving compare-and-set: a stale concurrent waiter that
  observed a conservative condition replays the already-delivered terminal
  outcome instead of erasing it.
- Stored-session attach is resilient to the Hermes reattach race: a JSON-RPC
  `4007 "session no longer live; retry resume"` during `session.resume`
  receives exactly one immediate retry of the identical resume (an
  attach/rebuild of the same stored identity, not a mutation retry); a second
  transient failure, and a genuine `4007 "session not found"`, return the
  normal structured error with no retry loop and no automatic session
  creation.

- Live completion attribution: in a shared attached session, `live_wait` no
  longer accepts the first `message.complete` after a sequence cursor as the
  local answer. A submit acknowledged `streaming` proves ownership of the
  running turn via the gateway inflight snapshot (`session.activate`, SHA-256
  of the stripped prompt text) and a post-proof watermark; a foreign turn's
  completion while the claimed turn is still running is skipped, and cases
  where ownership cannot be proven return conservative `ambiguous_turn` /
  `completion_not_observed` states instead of another client's answer.
  Residual known limitation: a byte-identical prompt from another attached
  writer cannot be distinguished (Hermes exposes no server-issued
  turn/admission ID), so if such a same-text turn replaces the local one in
  the terminal window — or inside an event-loss window on reconnect — its
  completion can be returned as the local answer. This is documented, not
  fixed; removing it requires the upstream turn-identity seam.
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
  gateway clears the inflight snapshot before emitting `message.complete`. On
  a proven-continuous connection a buffered candidate is accepted only when no
  inflight snapshot exists; while the claimed turn's healthy inflight snapshot
  is still present the candidate is skipped (the gateway cannot have emitted
  the claimed turn's completion yet).
- Any reconnect (even within the same replay epoch) invalidates the ownership
  proof: `live_wait` re-proves the claim via a fresh inflight snapshot and
  persists the re-proven cursor, or returns `completion_not_observed`. A
  reconnect that overlaps an already-running `live_wait` is caught too: the
  wait re-checks the connection generation and replay epoch before accepting
  any buffered candidate.
- Live `request_id` reservation is an atomic SQLite INSERT: concurrent calls
  with the same request_id can never submit two gateway mutations.
- A live foreign-turn inflight snapshot (a prompt that is not the claimed one)
  makes `live_wait` return `completion_not_observed` instead of accepting a
  buffered completion; acceptance requires the inflight snapshot to be absent.
  A retained FAILED-turn snapshot (the gateway keeps it, with an error marker,
  while emitting the terminal completion) is recognized as the local failure
  only for a terminal error candidate, reported as `failed`/`live_turn_failed`
  with no answer — even when the gateway's error payload carries fallback
  failure copy in `text`; that copy is surfaced through `error`, never as the
  answer. A success payload under a retained failure snapshot is a
  non-conforming ordering and stays conservative
  (`completion_not_observed`).
- A truncated or errored replay, or a replay-epoch change (a runtime rotation
  clears the buffered stream, which is the same kind of event-loss window),
  makes `live_wait` conservative (`completion_not_observed`) regardless of a
  successful re-proof: buffered ordering after event loss cannot attribute
  completions. The degradation marker is connection-wide — it is deliberately
  not keyed by the runtime session id, so it survives a reconnect/resume that
  rotates the id; durable recovery via `live_reconcile`/`live_history` is
  required. Only a clean same-epoch reconnect with a complete replay keeps
  waiting after re-proof.
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
