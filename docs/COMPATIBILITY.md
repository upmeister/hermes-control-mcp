# Compatibility and public-beta support tiers

This project intentionally separates the **durable core** from the optional
**shared live-session tier**.

## Public-beta capability tiers

| Capability | Public-beta status | Stock Hermes required | Extra integration |
|---|---|---|---|
| Durable Runs API | Stable | Yes | None |
| Durable multi-profile routing | Stable | Yes, with Hermes multiplex/profile API support | Per-profile API_SERVER_KEY |
| Durable status/history/control | Stable | Yes | None |
| Shared Desktop/TUI live attach | Experimental | Not on stock v0.21.3 | Compatible owner/native attach seam |
| Interactive approval/clarify server requests | Not implemented | N/A | Future security-reviewed design |
| Streamable HTTP MCP | Not implemented | N/A | Future remote-auth boundary |

The bridge's public beta is considered **ready** when the durable core is ready.
The live tier is optional unless a user explicitly requires it.

Use:

~~~bash
hermes-control-mcp doctor
~~~

to check the durable core and treat live as optional, or:

~~~bash
hermes-control-mcp doctor --require-live
~~~

to make unavailable live attach a hard failure.

## Hermes compatibility snapshot

Research and production verification date: 2026-09-20.

Known-good project evidence includes:

- Hermes stable v0.21.3 / v2026.9.14 for the API/profile generation this project
  was developed against;
- a deployed Hermes owner-adapter revision used for Stage 2/2.1/2.2 testing;
- current upstream main research for TUI profile parameters, multiplex API
  routing and transient resume semantics.

This is **not** a promise that every future Hermes revision is automatically
compatible. The bridge tracks upstream contracts and will adapt through patch
releases when Hermes changes session authority, native attach or profile
semantics.

See [UPSTREAM-HERMES.md](UPSTREAM-HERMES.md).

## Durable core prerequisites

Default profile:

- Hermes API Server is running;
- the bridge can resolve the configured default API key;
- /health, /v1/models and /v1/capabilities are reachable.

Named profiles:

- the named Hermes profile exists;
- Hermes is actually serving that profile through multiplex routing;
- the profile has its own API_SERVER_KEY;
- /p/<profile>/... is reachable with that profile key.

The bridge never borrows the default API key for a named profile.

Probe explicit profiles:

~~~bash
hermes-control-mcp doctor --profile coder --profile research
~~~

or discover syntactically valid named profile directories:

~~~bash
hermes-control-mcp doctor --all-profiles
~~~

Doctor only performs non-consuming readiness requests. It does not submit an
LLM run, prompt, steer, interrupt or configuration mutation.

## Hermes multiplexing caveat

Hermes multiplexing is an execution-topology choice, not just an API routing
switch.

On Hermes generations used by this project, explicitly enabling multiplexing
can activate configured platform adapters for multiple live profiles. Existing
installations that copied Telegram/Discord/other adapter credentials between
profiles may therefore surface duplicate-credential conflicts during gateway
startup.

Before explicitly forcing multiplex mode on an old installation:

1. review live named profiles;
2. review platform-adapter credentials;
3. remove/rotate accidental duplicates or disable unused adapters;
4. start Hermes and verify served profiles;
5. run bridge doctor.

The bridge detects API/profile readiness but deliberately does **not** rewrite
Hermes profile topology or adapter configuration.

A clean installation with intentionally configured profiles should not inherit
the legacy duplicate-credential problem by itself.

## Live shared-session tier

The live tier joins the same Hermes TUI/Desktop runtime instead of creating a
second authority.

Current public-beta status:

~~~text
experimental / optional
~~~

The currently deployed implementation uses a private owner-adapter lease and
Unix-domain WebSocket. That owner seam is still out-of-tree relative to stock
Hermes v0.21.3.

Therefore:

- installing the bridge on stock Hermes is enough for the durable core;
- it is **not** enough to promise shared live Desktop/TUI attach;
- live readiness must be checked separately;
- a missing/stale owner lease does not make the durable public beta unusable.

The project is tracking upstream native/session-authority work. If Hermes lands
a supported equivalent seam, the bridge should migrate to it rather than grow a
permanent private fork.

## State database contract

The public registry schema starts at version 1.

Opening a legacy unversioned database performs the existing additive migrations
and promotes it to schema v1.

A database with a schema version newer than the running bridge fails closed;
users must upgrade the bridge rather than letting older code mutate unknown
state.

### Process ownership

Public-beta contract:

~~~text
one bridge process owns one --state-db at a time
~~~

If several MCP hosts need independent bridge processes, give them separate
state-db paths unless they intentionally share one long-lived bridge process.

SQLite serializes individual writes, but the bridge has higher-level
check-then-admit semantics that are not advertised as multi-process safe in this
release.

## Python and packaging

Supported Python versions for the public beta:

~~~text
3.11
3.12
3.13
~~~

CI builds wheel + sdist and installs the wheel into a fresh virtual environment
before running an MCP stdio smoke test.

## Upstream policy

Open upstream PRs/issues are research inputs, not released dependencies.

The bridge roadmap explicitly watches Hermes native attach, unified gateway
authority and admission identity work. Public patch releases may change the live
adapter implementation while preserving the MCP-facing contract where
practical.
