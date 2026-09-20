# Distribution and upstreaming strategy

## Problem

A public user should not need private server paths, hand-copied source trees or
an undocumented Hermes fork to install the bridge.

At the same time, packaging choices must preserve the current architecture:
the MCP client owns the bridge process; Hermes owns agent/session execution.

## Option 1 — standalone Python package

### Shape

Publish the existing project as a normal Python package with a console entry
point:

~~~text
hermes-control-mcp
~~~

and ideally support an isolated runner workflow such as:

~~~text
uvx hermes-control-mcp ...
~~~

after release packaging is verified.

### Advantages

- matches current stdio MCP lifecycle;
- client-neutral;
- does not require Hermes to load arbitrary plugin code;
- independent release cadence;
- easiest path for SSH-wrapped remote use;
- clean separation between MCP control plane and Hermes runtime.

### Costs

- user must separately ensure Hermes API/owner prerequisites;
- version compatibility must be documented;
- owner attach remains awkward until upstream supplies a supported seam.

### Recommendation

**Primary public distribution.**

## Option 2 — Hermes plugin wrapper

Hermes general plugins support:

- Git installation through `hermes plugins install`;
- pip entry points under `hermes_agent.plugins`;
- plugin Python dependencies;
- `python_runtime: external` for sidecar-managed runtimes;
- registered CLI subcommands/tools/hooks.

Therefore a bridge plugin is technically possible.

### Good uses for a companion plugin

A future plugin could:

- add `hermes bridge doctor` / `hermes bridge print-mcp-config`;
- verify API Server/profile prerequisites;
- discover supported owner/gateway capabilities;
- install/locate the standalone bridge package;
- provide version compatibility diagnostics;
- expose a safe operator command to print non-secret connection metadata.

### Bad use

Do not move the entire MCP stdio server into the long-lived Hermes gateway just
to call it a plugin.

That would invert ownership:

~~~text
desired:
MCP client owns bridge process -> bridge attaches to Hermes

undesired:
Hermes owns plugin process -> plugin somehow waits for arbitrary MCP clients
~~~

It would also make client disconnect/restart semantics less obvious.

### Owner seam limitation

Current general agent-plugin APIs do not provide a straightforward supported
primitive to install the private TUI owner WebSocket/UDS route used by Stage 2.

Dashboard `plugin_api.py` routes are a different web process/namespace and do
not automatically share the TUI runtime authority.

So a plugin cannot currently eliminate the need for a Hermes-side owner/native
attach seam without depending on internals.

### Recommendation

Treat a Hermes plugin as an **optional installer/integration companion**, not the
primary runtime.

## Option 3 — merge the bridge into hermes-agent

This may eventually be the best end state, but timing matters.

### Reasons an upstream bridge could make sense

- MCP is a standard external-agent/tooling protocol;
- Hermes already supports several programmatic surfaces;
- an official bridge can track internal session/profile contract changes in the
  same repository;
- public users would get one installation/update path;
- the owner/native attach seam could be tested atomically with the client.

### Reasons not to propose the whole bridge immediately

- current live owner adapter is not stock upstream behavior;
- upstream is actively redesigning local session authority in PR #106742 and
  issue #109891;
- the bridge still has public-API decisions pending around multi-profile and
  Agent Sessions API;
- adding MCP server dependencies/lifecycle to Hermes core is a product decision,
  not merely a code donation;
- a standalone public beta will provide evidence of real external demand and
  stabilize the MCP method contract.

## Recommended upstream sequence

### Step 1 — upstream generic Hermes seams

Prefer small PRs that are independently useful to Hermes:

1. supported local machine/native attach or control endpoint;
2. stable admission/turn identity for ordinary prompt submission;
3. profile-bound discovery and resume;
4. scoped external-client grants/capabilities.

These are easier for Hermes maintainers to evaluate because they solve generic
runtime problems, not a single downstream integration.

### Step 2 — public bridge beta

Ship the standalone package once:

- Stage 2.2A recovery is fixed;
- multi-profile semantics are correct;
- owner requirement is explicit;
- package/license/CI are ready.

Collect real usage and compatibility evidence.

### Step 3 — propose official integration

Then open an upstream design issue/PR proposing one of:

- `hermes mcp` as an official command;
- a bundled Hermes plugin;
- moving this repository/module into hermes-agent;
- keeping it external but listing it as an official companion.

Let maintainers choose the product boundary.

## What should be upstreamed first from our current code?

Not the SQLite registry or MCP tool names.

The most valuable current contribution is the **owner/machine attach contract**
or the smallest version of it that aligns with upstream gateway authority.

Our bridge-specific reconciliation and MCP envelope can continue iterating
outside Hermes.

## Compatibility policy for public release

The package should eventually publish a matrix like:

| Bridge version | Hermes stable | Durable Runs | Agent Sessions | Live owner attach |
|---|---|---|---|---|
| 0.x | verified range | yes | experimental/optional | requires supported seam or documented patch |

Never claim live support for stock Hermes until the required owner/native seam is
actually available in a released or clearly supported upstream version.

## Decision

For Stage 2.2:

- build packaging around a standalone Python/MCP process;
- research, but do not implement, a companion Hermes plugin;
- prepare a focused upstream contribution strategy;
- do not open a whole-bridge main-repo PR until the public contract and upstream
  authority direction are clearer.
