# Stage 2.2C implementation brief — public-beta hardening

Status: **IN PROGRESS**

Behavioral base:

~~~text
0d5b8be4047b0f6084cea0a071144bfafd439ecc
~~~

This stage is release engineering and public-install hardening, not a new agent
runtime feature.

## Accepted product decisions

### Capability model

Public beta uses two tiers:

~~~text
durable core = stable / stock supported Hermes
live shared-session attach = experimental / optional
~~~

Unavailable live attach does not fail the default readiness gate.

Users who depend on live attach can require it explicitly.

### Distribution

Primary runtime remains a standalone Python MCP stdio package.

A Hermes plugin may later help installation/discovery, but does not own the
bridge process in this release.

### License

MIT.

### Naming

Product display name: **Hermes MCP Control Plane**.

Final distribution/repository/CLI slug remains a maintainer decision during this
stage. Do not publish under an unconfirmed name.

## Required work

### C1 — doctor

Add a non-consuming CLI readiness command.

Required UX:

~~~bash
<entrypoint> doctor
<entrypoint> doctor --profile coder
<entrypoint> doctor --all-profiles
<entrypoint> doctor --require-live
<entrypoint> doctor --json
~~~

Checks:

- default protected API readiness;
- named profile key presence;
- named profile protected API routing;
- optional live owner/native attach;
- no LLM run or prompt;
- no config mutation;
- no secret values in output.

Default success rule:

~~~text
durable ready + requested profiles ready = exit 0
live unavailable + not required = warning, still exit 0
~~~

### C2 — CI

GitHub Actions on Python 3.11/3.12/3.13:

- package install;
- full tests;
- unittest discovery;
- compileall / py_compile;
- git diff --check.

Separate package gate:

- build wheel + sdist;
- create a fresh venv;
- install the wheel, not the source tree;
- invoke installed console entrypoint;
- complete a real MCP stdio initialize + bridge_health/live_health smoke.

### C3 — registry schema ownership

Introduce public registry schema v1 through SQLite user_version.

- legacy version 0 migrates additively then becomes v1;
- future schema > supported fails closed;
- no generic migration framework is required yet.

Document one-process-per-state-db as the public-beta ownership contract.

### C4 — package metadata

- beta semantic version;
- MIT license metadata;
- supported Python classifiers;
- project URLs;
- client-neutral description/keywords;
- final distribution/CLI naming after maintainer confirmation.

### C5 — compatibility docs

Document:

- durable stable vs live experimental tiers;
- stock-Hermes limitation of live owner attach;
- multi-profile API/key prerequisites;
- Hermes multiplexing adapter side effects on legacy installations;
- doctor workflow;
- upstream adaptation policy.

### C6 — README / roadmap / changelog

Make the root README sufficient for a third party to understand:

- what the project is;
- what works on stock Hermes;
- what live attach requires;
- install/run/doctor;
- multi-profile prerequisites;
- state-db ownership;
- upstream caveats;
- license and roadmap.

## Non-goals

- publish to PyPI from this PR;
- create an HTTP MCP listener;
- interactive approvals/clarify;
- generic cross-platform owner IPC;
- Agent Sessions migration;
- Hermes config/profile mutation;
- auto-enable multiplexing;
- auto-repair duplicate platform credentials;
- Stage 3 A2A.

## Release acceptance

Before merge:

- full CI green;
- clean installed-wheel smoke green;
- doctor unit tests green;
- no secret canaries in output;
- docs contain no private deployment host/path;
- exact package name and CLI confirmed;
- MIT LICENSE present;
- public README marks live as experimental/optional;
- public README does not imply stock Hermes ships the owner adapter.

Before actual public publication:

- repository visibility decision;
- final package/repository rename;
- PyPI name availability rechecked immediately before upload;
- tag/release notes;
- no production secret or private operational artifact in tracked history.
