# Hermes MCP Control Plane

**Hermes MCP Control Plane** exposes [Hermes Agent](https://github.com/NousResearch/hermes-agent) as a controllable MCP agent: durable runs, profile-aware routing, and optional shared live sessions with an existing Hermes Desktop/TUI runtime.

~~~text
MCP host
   |
   v
hermes-control-mcp
   |-- durable --> Hermes API Server
   |
   '-- live ----> existing Hermes TUI gateway runtime
                  (optional / experimental)
~~~

The bridge is deliberately a control plane, not a second Hermes runtime. Hermes remains the authority for sessions, models, tools, approvals, profiles, and execution.

## Status

| Capability | Public beta |
|---|---|
| Durable run submit/status/wait/events | **Stable** |
| Durable stop/steer/history | **Stable** |
| Multi-profile routing | **Stable** |
| Shared Desktop/TUI live attach | **Experimental / optional** |
| Interactive approval/clarify requests | Not implemented |
| Remote Streamable HTTP MCP | Not implemented |

The durable tier works against supported stock Hermes API Server deployments. Shared live attach currently requires a compatible owner/native attach seam and is intentionally not required for the public beta.

See [Compatibility](docs/COMPATIBILITY.md) for the exact boundary.

## Install

Python 3.11–3.13 is supported.

~~~bash
pip install hermes-control-mcp
~~~

Or from source:

~~~bash
git clone https://github.com/upmeister/hermes-control-mcp.git
cd hermes-control-mcp
python -m venv .venv
. .venv/bin/activate
pip install -e .
~~~

## Quick start

Hermes API Server must already be running. The default target is `http://127.0.0.1:8642`.

Keep API keys in environment/server-side configuration, not MCP arguments.

~~~bash
export API_SERVER_KEY='...'
hermes-control-mcp doctor
~~~

A healthy durable-only deployment may report the live tier as unavailable and still return `READY`.

To make live attach mandatory:

~~~bash
hermes-control-mcp doctor --require-live
~~~

### MCP host configuration

~~~json
{
  "mcpServers": {
    "hermes": {
      "command": "hermes-control-mcp"
    }
  }
}
~~~

A generic example is available at [`examples/mcp-stdio.json`](examples/mcp-stdio.json).

## MCP surface

Durable tools:

- `run_start`, `run_status`, `run_wait`, `run_events`
- `run_stop`, `run_steer`
- `session_history`
- `bridge_health`

Optional live tools:

- `live_session_open`
- `live_prompt`, `live_wait`, `live_events`
- `live_status`, `live_history`
- `live_steer`, `live_interrupt`
- `live_reconcile`, `live_reconnect`, `live_health`

The bridge does **not** expose arbitrary shell execution, raw gateway RPC, slash commands, Hermes config mutation, or credential mutation.

## Multi-profile Hermes

Profiles are first-class routing boundaries:

~~~text
(profile, lane) -> stored_session_id
~~~

For a named profile such as `coder`:

- durable API calls use Hermes `/p/coder/...` routes;
- the profile uses its own `API_SERVER_KEY`;
- live create/resume/control preserves the same profile;
- the default profile key is never borrowed for a named profile.

Probe profiles before connecting an MCP host:

~~~bash
hermes-control-mcp doctor --profile coder
hermes-control-mcp doctor --profile coder --profile research
hermes-control-mcp doctor --all-profiles
~~~

If an omitted profile can be inferred from exactly one existing local identity, the bridge reuses it. Ambiguous cross-profile routing fails closed instead of guessing.

## Recovery and safety

- one logical durable request keeps one idempotency identity;
- uncertain mutations are reconciled rather than blindly resubmitted;
- stored session IDs and runtime session IDs remain distinct;
- reconnect/replay never proves ownership of a foreign completion by itself;
- failed/interrupted live turns return no answer payload;
- credentials and raw prompts are not persisted in the bridge registry;
- named-profile credentials never fall back to the default profile.

The public registry schema starts at version 1. Public-beta process ownership is **one bridge process per state DB**.

## Live attach caveat

Shared live attach is currently **experimental**.

The deployed implementation joins the existing Hermes TUI gateway through a private local owner boundary. Stock Hermes v0.21.3 does not ship that project-specific owner seam.

Installing this package is therefore sufficient for the durable tier, but not by itself a promise that shared Desktop/TUI attach is available.

The project is tracking Hermes upstream native/session-authority work and intends to adapt the live transport when a supported upstream seam lands rather than maintain a permanent competing runtime.

See [upstream research](docs/UPSTREAM-HERMES.md).

## Hermes multiplexing note

Explicit Hermes multiplexing can activate configured platform adapters across multiple live profiles. Older installations with copied Telegram/Discord/etc. credentials may surface duplicate-credential conflicts during gateway startup.

The bridge detects profile/API readiness but deliberately does not rewrite Hermes profile topology or adapter configuration.

See [Compatibility](docs/COMPATIBILITY.md#hermes-multiplexing-caveat).

## Development

~~~bash
pip install -e .
./scripts/test.sh
python -m unittest discover -s tests -v
python -m compileall -q src
python -m py_compile src/hermes_control_mcp/*.py
git diff --check
~~~

CI tests Python 3.11, 3.12, and 3.13, then builds wheel + sdist, installs the wheel into a clean virtual environment, and performs an MCP stdio smoke from the installed console entrypoint.

## Documentation

- [Compatibility and support tiers](docs/COMPATIBILITY.md)
- [Roadmap](docs/ROADMAP.md)
- [Hermes upstream research](docs/UPSTREAM-HERMES.md)
- [API Server / Agent Sessions research plan](docs/API-SERVER-PARITY-SPIKE.md)
- [Distribution and upstreaming strategy](docs/DISTRIBUTION-AND-UPSTREAMING.md)
- [Release process](docs/RELEASING.md)
- [Security policy](SECURITY.md)

## Security

Please do not report credential leaks, auth-boundary bypasses, or cross-profile isolation bugs in a public issue. Use GitHub's **Private vulnerability reporting** for this repository.

See [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
