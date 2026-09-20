# Hermes upstream research snapshot

Research date: **2026-09-20**.
Fresh current-main SHA rechecked after the first public bridge release:
`59f9ff8dbc75b9c4f07ae10174df730f7882a505`.

The earlier Stage 2.2B handoff was checked against
`8a92051f20e6b371c4ff1a46a5bcec7138cc4e8c`; the relevant profile/session
contracts below remain unchanged in the post-release recheck.

This document records facts relevant to hermes-control-mcp. It deliberately
separates stable release behavior, current-main observations and open proposals.

Upstream changes quickly; re-verify before implementing against an internal
field or assuming an open proposal landed.

## Stable release

Latest stable release observed during this research:

~~~text
Hermes Agent v0.21.3
tag: v2026.9.14
release date: 2026-09-14
~~~

The release notes describe a large roll-up since v0.21.2, including remote
Desktop auth fixes, state.db handle fixes and additional undocumented-at-release
work around gateway contracts and multiplex profile isolation.

## API Server: important changes for this project

Current Hermes documentation exposes three relevant programmatic families:

- OpenAI-compatible endpoints;
- Runs API;
- Agent Sessions API.

The Agent Sessions API now includes:

~~~text
GET    /api/sessions
POST   /api/sessions
GET    /api/sessions/{id}
PATCH  /api/sessions/{id}
DELETE /api/sessions/{id}
GET    /api/sessions/{id}/messages
POST   /api/sessions/{id}/fork
POST   /api/sessions/{id}/chat
POST   /api/sessions/{id}/chat/stream
~~~

The streaming session-turn endpoint emits agent progress and a terminal run
outcome. This makes it a plausible durable/session-oriented substrate for some
future bridge operations.

It does **not** by itself prove parity with live owner attach: the bridge still
needs the TUI owner path when the requirement is to participate in the exact
same Desktop/TUI runtime and event stream.

See [API-SERVER-PARITY-SPIKE.md](API-SERVER-PARITY-SPIKE.md).

## Multiplex profiles

Hermes current main treats multiplexing as a first-class gateway topology.

The API Server serves named profiles under:

~~~text
/p/<profile>/...
~~~

Examples:

~~~text
/p/coder/v1/runs
/p/coder/api/sessions
/p/coder/api/sessions/<id>/chat/stream
~~~

Important routing/auth properties verified on current main:

- the named profile prefix expects that profile's own `API_SERVER_KEY`;
- the default profile key is not a universal credential for named prefixes;
- missing named-profile key fails closed rather than inheriting the owner key;
- unknown/unserved profile prefixes fail closed;
- Runs idempotency is scoped upstream by profile/principal identity.

This makes Stage 2.2B multi-profile routing a correctness/security requirement
for a public bridge, not a convenience feature.

## TUI session profile semantics

Current TUI contracts make profile broader than a create/resume option.

`session.create` inherits `ProfileParams`. `SessionParams` contains both
`session_id` and optional `profile`, and current session-addressed methods
inherit it. That covers resume/activate/status/history/interrupt/events and
ordinary prompt submission.

Bridge implication:

- passing profile only on create is insufficient;
- profile must survive bridge process restart and accompany resume;
- profile must also follow live status/history/prompt/control/replay requests so
  a stored/runtime ID is never resolved under the wrong profile scope.

Current canonical profile IDs are lowercase and match
`^[a-z0-9][a-z0-9_-]{0,63}$`; `default` is the special default alias. Hermes
remains authority for profile existence/reserved names.

## 4007 session lifecycle semantics

Current Hermes TUI lifecycle includes the explicit transient response:

~~~text
4007 "session no longer live; retry resume"
~~~

It occurs when a live-session record becomes stale/retired during reattach.

This is distinct from:

~~~text
4007 "session not found"
~~~

which is a genuine missing target.

The bridge should narrowly retry the first condition and must not interpret all
4007 errors as "create a new session".

Stage 2.2A implemented this exact bounded retry in bridge PR #5; independent
review confirmed the predicate still matches current upstream literally.

## Ordinary prompt identity limitation

As of this snapshot, the ordinary TUI `PromptSubmitResult` does not expose a
general server-issued admission/turn ID that is guaranteed to be echoed through
the ordinary terminal message event.

Hermes has turn IDs in specialized areas such as hosted-room/compute flows, but
that does not remove the Stage 2.1 shared-writer limitation for the normal
prompt path.

Bridge consequence:

Byte-identical competing prompts from multiple writers can remain
indistinguishable in a narrow terminal/reconnect window. Stage 2.1 mitigates
every distinguishable case but cannot invent missing server identity.

## Open PR #106742 — unified gateway authority

Status at the post-release recheck: **open, not merged**.

Title/theme: one gateway owns every local session.

The PR is directly relevant because it proposes/implements a much broader
version of several bridge goals:

- one canonical gateway session authority;
- durable admissions and exact retry identity;
- local surfaces attaching instead of competing writers;
- shared approvals/queues/events;
- profile-scoped authorities under multiplexing;
- POSIX socket / Windows named-pipe local access;
- gateway discovery/generation semantics.

The PR is strong implementation evidence, not stable product behavior.

Bridge strategy:

- do not rebuild its cross-platform local authority stack in parallel;
- if it or a staged derivative lands, evaluate replacing the private owner
  adapter with the supported upstream boundary.

## Open issue #109891 — gateway as Desktop backend

Status at the post-release recheck: **open**.

The proposal argues for a first-class gateway backend for Desktop and explicitly
separates:

- local native control transport; and
- authenticated remote HTTP/WebSocket transport.

That separation closely matches the bridge's preference to keep local
same-user owner attach distinct from remote network authentication.

## Open issue #62857 — scoped native WebSocket grants

Status at research time: **open**.

The issue proposes capability-limited native grants such as:

~~~text
conversation.read
conversation.write
conversation.control
~~~

with a deliberately narrow method set.

This matters for a future public bridge because the existing full gateway
surface is broader than the MCP allowlist.

Do not assume this authorization model exists until it is merged.

## Hermes plugin system findings

Hermes general plugins can currently be distributed from several sources,
including:

- user/project plugin directories;
- Git repositories through `hermes plugins install`;
- pip entry points under `hermes_agent.plugins`.

Plugin manifests support Python dependencies and an
`python_runtime: external` sidecar model.

Plugins can register agent tools, hooks, slash commands and CLI subcommands.

However, the general agent-plugin API does not currently expose a simple
`ctx.register_server` / `ctx.register_asgi_route` primitive for adding the
private TUI owner transport seam used by this bridge. Dashboard plugins have
their own `plugin_api.py` HTTP namespace, but that is a different runtime and
trust boundary.

Conclusion:

A Hermes plugin can be a useful **distribution/installation companion** for the
bridge, but it is not a drop-in substitute for an upstream-supported owner
attach contract.

## Owner adapter status

The bridge's current private owner adapter is an out-of-tree Hermes integration.

Do not write documentation implying that stock Hermes v0.21.3/current main
ships `/api/owner/ws` or our owner lease format.

This distinction is a precondition for public release.

## Upstream contribution strategy

Near-term upstream candidates should be generic Hermes seams:

1. supported machine/native local attach;
2. stable session/admission identity;
3. profile-bound discovery/routing;
4. scoped external-client authorization.

Only after those stabilize should we decide whether the MCP bridge itself
belongs in Hermes core.

See [DISTRIBUTION-AND-UPSTREAMING.md](DISTRIBUTION-AND-UPSTREAMING.md).
