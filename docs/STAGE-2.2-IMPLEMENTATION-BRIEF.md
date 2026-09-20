> **Status: historical / complete.** Stage 2.2A was implemented by PR #5
> and squash-merged as `999ccff928257ea072f956e657a7c98bf71fb8c0`.
> Final receipt: 79 tests; Pytna remediation/reread PASS; independent review PASS.
> The current coding contract is
> [STAGE-2.2B-IMPLEMENTATION-BRIEF.md](STAGE-2.2B-IMPLEMENTATION-BRIEF.md).

# Stage 2.2A implementation brief — lifecycle recovery

This is the exact contract for the next coding PR.

## Base

Start from current `main`. At the time this brief was written, the merged
Stage 2.1 head is:

~~~text
b786e6da83db575f6637897214c1c4442c2d867c
~~~

Before coding, re-read remote main and record the actual base SHA in the PR.

## Objective

Close two recovery gaps without expanding the protocol surface:

1. a conservative `live_wait` result can currently tell the caller to use
   `live_reconcile` while leaving the registry record in a state that
   `live_reconcile` refuses;
2. Hermes may transiently return JSON-RPC 4007 with the exact semantic
   "session no longer live; retry resume" during a reattach/reaper race.

The PR must make those cases recoverable while preserving every Stage 2.1
false-positive safety invariant.

## Scope A — wait → reconcile state transition

### Current failure mode

A live request may remain stored as `streaming` / `claimed`.

`live_wait` can later return a conservative result such as:

~~~text
status=unknown
error_code=completion_not_observed
use live_reconcile/live_history
~~~

but the registry row is not always changed to `status=unknown`.

`live_reconcile` currently accepts only registry rows whose status is
`unknown`. The user-visible recovery advice can therefore lead to an immediate
non-reconciliation replay of the stale `streaming` row.

### Required behavior

Whenever `live_wait` gives up ownership/completion attribution and explicitly
hands the request to durable recovery, persist that recovery state before
returning.

At minimum this includes exits whose public result is:

- `completion_not_observed`;
- `ambiguous_turn` when the response tells the caller ownership cannot be
  proven and recommends reconciliation.

Do **not** turn an ordinary bounded `wait_timeout` into unknown merely because
the turn is still running.

Recommended implementation shape:

- add one small helper in `LiveService` that marks the request
  `status=unknown` with the conservative error code before building the
  result;
- route the relevant recovery exits through it;
- keep terminal `completed`, `failed`, `interrupted`, `reconciled`
  replay behavior unchanged.

Do not broaden `live_reconcile` to arbitrary active streaming requests unless
the implementation proves that doing so cannot prematurely convert a running
turn into the bridge's terminal `reconciled` state.

### Required regression

Commit an end-to-end control:

~~~text
submit
-> ownership proof
-> event/reconnect loss that makes live_wait conservative
-> live_wait returns unknown/completion_not_observed
-> durable post-boundary user row exists
-> live_reconcile returns reconciled
~~~

The same test should be RED on the Stage 2.1 base for the expected reason.

Also add a focused unproven/ambiguous recovery test if the chosen helper covers
that path.

## Scope B — transient Hermes 4007 retry-resume

### Upstream contract

Current Hermes TUI lifecycle code distinguishes at least two 4007 semantics:

~~~text
4007 "session no longer live; retry resume"
4007 "session not found"
~~~

The first is a transient race: resume found/referenced a live record that was
retired before reattach completed. The message explicitly instructs the client
to retry resume.

The second is a genuine missing durable target and must remain fail-closed.

### Required bridge behavior

Inside the stored-session branch of `LiveService.open()`:

1. call `session.resume` normally;
2. if and only if the reply raises `LiveRPCError` with:
   - rpc code 4007; and
   - the upstream transient semantic "session no longer live; retry resume"
     (match narrowly; normalize harmless case/whitespace only if useful),
   perform **one** immediate retry of the exact same `session.resume`;
3. never loop;
4. never replace resume with session.create;
5. if retry fails, return the normal structured error;
6. genuine "session not found" receives zero automatic retries.

The retry is allowed because it repeats an attach/rebuild operation against the
same stored identity; it is not permission to retry `prompt.submit`,
`steer`, `interrupt` or any other mutation.

### Required regressions

Add deterministic fake-gateway tests for:

1. transient 4007 once → second resume succeeds → lane rebinds to the new runtime;
2. transient 4007 twice → exactly two total resume calls, then structured failure;
3. genuine 4007 "session not found" → exactly one resume call, no create;
4. no regression to the existing stored/runtime identity split.

If practical, add a disposable owner fixture that retires a live runtime between
resume lookup and reattach. Do not make the PR depend on a flaky timing race if
the deterministic contract test is stronger.

## Empty draft semantics

Hermes intentionally does not persist an empty fresh TUI draft until there is
real transcript intent.

Therefore a lane may point at a stored-looking key that never became durable if:

~~~text
live_session_open
-> no prompt/seeded history
-> bridge/client exits
-> runtime is reaped
~~~

A later genuine `4007 "session not found"` is valid in this case.

For this PR:

- do not auto-create;
- do not silently replace the lane with a new conversation;
- preserve a clear structured failure.

A dedicated public error such as `lane_session_gone` may be considered in a
later API-cleanup PR; it is not required here.

## Must-not-change invariants

- no blind retry after uncertain prompt acknowledgement;
- no acceptance of the first buffered completion as ownership proof;
- reconnect invalidates prior proof;
- degraded replay remains conservative;
- failed/interrupted turns have no answer;
- same-text competing-writer limitation remains documented;
- registry stores no raw prompt/body/credential;
- MCP tool list is unchanged;
- owner auth model is unchanged;
- no multi-profile schema change in this PR.

## Files likely involved

Expected:

~~~text
src/hermes_control_mcp/live_service.py
tests/test_live.py
README.md and/or CHANGELOG.md if behavior wording changes
~~~

Avoid unrelated refactors in `live_client.py` or registry schema unless the
implementation genuinely requires them.

## Evidence contract

Before requesting review:

~~~bash
./scripts/test.sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
python3 -m py_compile src/hermes_control_mcp/*.py
git diff --check
~~~

For every new behavioral regression, document RED on the exact Stage 2.1 base
and GREEN on the candidate when practical.

The PR body must state:

- base SHA;
- candidate SHA;
- exact number of tests;
- focused RED/GREEN controls;
- whether any real owner/UDS smoke was run;
- no production deploy/restart unless separately authorized.

## Review checklist

Reviewer should explicitly inspect:

- every conservative live_wait exit for registry/result consistency;
- whether any retry predicate catches genuine not-found;
- exact retry count;
- whether errors retain safe/redacted data;
- whether request/session identity survives runtime rotation;
- whether the patch accidentally changes profile semantics before Stage 2.2B.
