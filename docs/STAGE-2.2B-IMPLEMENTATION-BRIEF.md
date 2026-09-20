# Stage 2.2B implementation brief — first-class multi-profile routing

This is the exact contract for the next coding PR.

## Base

Start from current `main`.

Planning baseline at the time of this brief:

~~~text
999ccff928257ea072f956e657a7c98bf71fb8c0
~~~

That commit is the Stage 2.2A squash merge.

Before coding, refresh remote main and record the actual base SHA.

## Objective

Make Hermes profile identity a first-class bridge routing and security boundary
for both durable API work and live TUI sessions.

The public invariant after this PR is:

~~~text
(profile, lane) -> stored_session_id
~~~

not:

~~~text
lane -> whichever profile ambient process state happens to select
~~~

A named-profile request must never silently fall through to the default
profile's state, credentials, session database or API route.

## Source-verified Hermes contracts

Re-verify these against both the deployed Hermes revision and fresh upstream
before implementation.

### Profile syntax

Current Hermes canonical profile IDs are lowercase ASCII and match:

~~~text
^[a-z0-9][a-z0-9_-]{0,63}$
~~~

`default` is the special default profile alias.

The bridge may mirror this syntax for path/routing safety, but Hermes remains
the authority for profile existence and reserved names.

### TUI gateway

Current TUI contracts make `profile` part of `SessionParams`.

That includes, among others:

- `session.resume`;
- `session.activate`;
- `session.status`;
- `session.history`;
- `session.interrupt`;
- correction/steer methods;
- `session.events.since`;
- `prompt.submit`.

`session.create` also accepts `profile`.

Therefore profile must survive bridge restart and accompany resume/control; it
cannot remain a create-only option.

### API Server multiplexing

Current Hermes multiplex API Server mirrors routes under:

~~~text
/p/<profile>/...
~~~

Examples:

~~~text
/p/coder/v1/runs
/p/coder/v1/runs/<run_id>
/p/coder/api/sessions
~~~

Unknown/unserved profile prefixes fail closed.

For a named profile, Hermes resolves that profile's own `API_SERVER_KEY`.
The default profile key is not inherited as a universal named-profile key.

The default/unprefixed listener remains the default profile route.

### Runs idempotency

Hermes scopes Runs API idempotency by the request principal/profile boundary.

The bridge may remain stricter in Stage 2.2B: caller `request_id` and explicit
`idempotency_key` can stay bridge-global for backward compatibility.

However:

- profile must be stored on the request;
- profile must participate in the request fingerprint/conflict check;
- a request replay may never cross profile boundaries.

Relaxing the bridge-global idempotency-key namespace, if desired, belongs to
the later registry/release migration work.

## Public profile semantics

Canonical profile value:

~~~text
default
~~~

when a new operation omits `profile`.

For an existing local identity, omission may infer a profile only when the
mapping is unambiguous.

### Lane resolution

When `profile` is supplied:

- canonicalize/validate it;
- resolve exactly `(profile, lane)`;
- never fall back to another profile.

When `profile` is omitted and the lane already exists:

- exactly one bound profile -> infer it;
- more than one bound profile -> fail
  `lane_profile_ambiguous` and require an explicit profile;
- no binding -> use `default` for creation/start.

This lets a caller open:

~~~text
live_session_open(profile="coder", lane="repo-review")
~~~

and then use lane-only followups while that lane exists in only one profile,
without sacrificing correctness if another profile later uses the same lane.

### Exact run/session/request resolution

If a local request/run/session record already carries a profile:

- omitted profile may infer the recorded profile;
- a supplied different profile is a conflict, not a reroute.

If an external exact run/session ID is not known locally:

- supplied profile routes to that profile;
- omitted profile routes to `default` for backward compatibility.

If multiple local rows make an exact ID/profile relationship ambiguous, fail
closed rather than choosing one.

## Scope A — registry identity and migration

### Lane bindings

Introduce a profile-aware binding store.

Recommended shape:

~~~sql
lane_bindings(
  profile TEXT NOT NULL,
  lane TEXT NOT NULL,
  session_id TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY(profile, lane)
)
~~~

Migrate existing legacy `lanes` rows to:

~~~text
profile = default
~~~

Requirements:

- migration is idempotent;
- no existing default lane is lost;
- keep the legacy `lanes` table during 2.2B if that materially improves
  rollback safety;
- if legacy dual-write is used, only default-profile bindings belong there;
- named-profile bindings must never collapse into the legacy global lane key.

Do not build the full generic migration framework in this PR.

### Durable request rows

Add redacted profile identity to durable request rows.

Legacy rows become `default`.

Request fingerprints include canonical profile.

Keep caller request IDs globally unique.

It is acceptable for explicit idempotency keys to remain bridge-global in this
PR as the conservative compatibility contract; document that policy.

### Live request rows

Add profile identity to live request rows.

Legacy rows become `default`.

Reconciliation, reconnect and runtime rotation use the stored request profile;
they must not infer it from current ambient process state.

### Runtime maps

Process-local runtime routing must distinguish profile + lane.

Do not keep:

~~~text
lane -> runtime
~~~

as the only live key once duplicate lane names across profiles are legal.

## Scope B — live TUI profile propagation

### Open/create/resume

`live_session_open` already exposes `profile`, but Stage 2.2B must make it
durable.

For a new session:

~~~json
{"method":"session.create","params":{"profile":"coder", ...}}
~~~

For a stored-session resume:

~~~json
{"method":"session.resume","params":{"session_id":"<stored>","profile":"coder", ...}}
~~~

The Stage 2.2A bounded transient-4007 retry must retry the **same profile-scoped
resume params**, not drop profile on retry.

### Activate / status / history / prompt / controls / replay

Where the upstream method accepts `SessionParams`, pass the resolved profile.

This includes the live calls used by the bridge for:

- activation and ownership proof;
- prompt submit;
- status;
- history;
- event replay;
- steer/correction;
- interrupt.

Profile propagation should be systematic rather than one-off special cases.

### Reconnect

`live_reconnect` must reopen every remembered lane under its stored profile.

A reconnect may rotate runtime IDs but must not rotate profile identity.

### Reconcile

`live_reconcile(request_id=...)` resolves profile from the stored live request
record and resumes/reads durable history in that same profile.

No profile argument is required for reconcile because request identity already
owns the scope.

## Scope C — durable API profile routing

### URL routing

For canonical `default`:

~~~text
<api_url>/v1/...
~~~

For named profile `coder`:

~~~text
<api_url>/p/coder/v1/...
~~~

Apply the same prefix rule to all durable bridge API calls:

- runs submit/status/events/stop/steer;
- session history;
- models/capabilities/health where the route is mirrored/supported.

Do not concatenate a second profile prefix onto an already profile-pinned base
URL.

For this PR, if named-profile routing is requested while `api_url` is itself
configured with an incompatible non-root path, fail with a clear configuration
error rather than guessing.

### Named-profile API credentials

A named profile must use its own `API_SERVER_KEY`.

Recommended bridge configuration:

- keep current default-profile resolution unchanged;
- add a non-secret `profiles_root` path, defaulting to
  `$HERMES_HOME/profiles` for the normal default Hermes home;
- named profile key is read from
  `<profiles_root>/<profile>/.env`;
- do not fall back from a named profile to the process/default
  `API_SERVER_KEY`.

This is intentionally fail-closed.

Do not:

- expose API keys as MCP arguments;
- persist them in the bridge registry;
- put them in URLs;
- include them in errors/logs;
- silently borrow the default key for a named profile.

If the project chooses a different named-profile secret resolver, it must
preserve those properties and be simpler to audit.

### API client lifetime

The current API client caches one default API key at construction.

Stage 2.2B must remove that single-principal assumption.

Acceptable designs include:

- resolve URL + key per request; or
- maintain an internal profile-keyed client pool whose configuration contains
  no logged secret values.

Prefer the smaller design.

Key rotation should not require bridge state migration.

## Scope D — MCP contract

Add optional `profile` routing to lane/run/session operations that need it.

At minimum durable tools:

- `run_start`;
- `run_status`;
- `run_wait`;
- `run_events`;
- `run_stop`;
- `run_steer`;
- `session_history`;
- `bridge_health` if profile-specific probing is implemented.

Live tools that route by lane/session should accept or infer profile as needed:

- `live_session_open` (already has it);
- `live_prompt`;
- `live_wait` when lane-addressed;
- `live_events`;
- `live_status`;
- `live_history`;
- `live_steer`;
- `live_interrupt`.

`live_reconcile` infers profile from request ID.

`live_reconnect` replays all stored profile/lane bindings and needs no profile
argument.

Results should include canonical `profile` where it materially identifies the
target.

Tool descriptions must make omission/inference/ambiguity semantics explicit.

## Scope E — validation and errors

Add structured errors rather than fallback:

- `invalid_profile`;
- `lane_profile_ambiguous`;
- `lane_profile_conflict` or equivalent when supplied profile disagrees with
  an existing exact binding;
- a clear named-profile API-key/config error;
- preserve upstream 401/404 errors without leaking secret values.

Do not let profile strings become filesystem traversal or raw URL path input.

## Required tests

### Registry / migration

1. legacy lane migrates to `default`;
2. legacy durable/live request rows read as `default`;
3. `default:work` and `coder:work` can coexist;
4. lane-only lookup with one profile infers it;
5. lane-only lookup with two profiles returns `lane_profile_ambiguous`;
6. invalid/path-traversal profile is rejected before filesystem/network use.

### Live

7. named create passes profile;
8. bridge restart + named lane resume passes the same profile;
9. transient 4007 retry preserves identical session ID **and profile**;
10. prompt/activate/status/history/steer/interrupt carry the resolved profile;
11. reconnect reopens two same-named lanes in different profiles correctly;
12. reconcile reads history under the request's stored profile;
13. no named-profile operation falls back to default when binding exists.

### Durable API

14. default run uses unprefixed route + default key;
15. named run uses `/p/<profile>/v1/runs` + named key;
16. status/events/stop/steer remain on the originating profile;
17. named session_history uses named prefix/key;
18. missing named key fails closed and does not send the default key;
19. unknown/unserved named profile produces a structured error;
20. same request_id with a different profile conflicts rather than replaying;
21. profile participates in request fingerprint;
22. explicit run ID with known local profile cannot be rerouted to another
    supplied profile.

### Secret boundary

23. registry contains profile names but no API key;
24. errors/log fixtures contain no API key canaries;
25. MCP tool JSON/descriptions contain no secret values.

### Existing safety

All Stage 2.1/2.2A tests remain green, especially:

- no blind prompt mutation retry;
- shared-turn attribution;
- replay degradation;
- wait -> reconcile;
- transient 4007 bounded retry;
- genuine not-found no-create.

## Real two-profile semantic smoke

After merge/deploy authorization, run a bounded smoke against a disposable or
safe test profile pair.

At minimum:

1. default and one named profile have distinguishable session/history state;
2. durable run through default lands only in default;
3. durable run through named prefix lands only in named profile;
4. live create/prompt in named profile appears in that profile's history;
5. bridge restart/resume returns to the same named profile;
6. identical lane name may exist in default and named profile without
   cross-talk;
7. wrong/default API key on named prefix is rejected;
8. no raw secret reaches bridge registry/log output.

Do not report this as CI.

## Non-goals

This PR does not:

- migrate durable work from Runs API to Agent Sessions API;
- implement Streamable HTTP MCP;
- implement interactive server requests/approvals;
- add cross-platform owner IPC;
- upstream the owner adapter;
- publish to PyPI;
- choose/ship a LICENSE;
- build the generic registry migration framework;
- relax bridge-global request/idempotency identifiers;
- begin Stage 3 A2A orchestration.

Those remain separate milestones/research.

## Likely owner files

Expected:

~~~text
src/hermes_control_mcp/config.py
src/hermes_control_mcp/api.py
src/hermes_control_mcp/registry.py
src/hermes_control_mcp/service.py
src/hermes_control_mcp/live_service.py
src/hermes_control_mcp/mcp_server.py
src/hermes_control_mcp/server.py
tests/test_*.py
README.md
CHANGELOG.md
~~~

A wider file list is acceptable only when directly required by profile routing.

Do not refactor live protocol internals for aesthetics.

## Evidence contract

Before review:

~~~bash
./scripts/test.sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
python3 -m py_compile src/hermes_control_mcp/*.py
git diff --check
~~~

The PR receipt must record:

- exact bridge base/head;
- exact deployed Hermes revision checked;
- fresh upstream SHA checked;
- profile-routing contracts source-verified;
- migration behavior;
- default + named HTTP route/key controls;
- live restart/resume controls;
- secret canary results;
- full test count;
- no deploy/restart unless separately authorized.

## Pytna review focus

High-value attacks:

1. named lane resumes in default after process restart;
2. default API key is accidentally sent to `/p/coder/`;
3. same lane name in two profiles routes nondeterministically;
4. supplied profile conflicts with stored request/lane profile but is silently
   accepted;
5. transient 4007 retry drops profile on retry;
6. reconcile opens default history for a named request;
7. explicit run ID is controlled through the wrong profile prefix;
8. migration loses an existing default lane;
9. profile path traversal reaches dotenv/filesystem resolution;
10. secret canary appears in registry/error/log;
11. profile addition weakens Stage 2.1 request attribution or 2.2A recovery.

Recommendation:

~~~text
PASS-TO-MERGE | REMEDIATE | BLOCKED-FOR-MAINTAINER
~~~

Default workflow remains one implementation pass, one adversarial review and
remediation only for concrete findings.
