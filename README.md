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

Python 3.11–3.14 is supported.

For a CLI application, an isolated tool environment is the recommended install:

~~~bash
uv tool install hermes-control-mcp
~~~

If you prefer plain `pip`, install inside a virtual environment:

~~~bash
python3 -m venv ~/.venvs/hermes-control-mcp
~/.venvs/hermes-control-mcp/bin/python -m pip install --upgrade pip
~/.venvs/hermes-control-mcp/bin/pip install hermes-control-mcp
~~~

On Debian/Ubuntu and other PEP 668 systems, running `pip install` directly
against the system Python may fail with `externally-managed-environment`.
That is an operating-system packaging guard, not a Hermes MCP compatibility
error. Do not use `sudo pip` or `--break-system-packages`; use `uv tool`,
`pipx`, or a virtual environment instead.

From source:

~~~bash
git clone https://github.com/upmeister/hermes-control-mcp.git
cd hermes-control-mcp
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
~~~

## Quick start

Hermes MCP Control Plane does **not** start or configure Hermes for you. Before
the bridge can connect, Hermes must have its API Server enabled and the bridge
must know both the API URL and the matching `API_SERVER_KEY`.

Choose your topology first:

| Bridge location | What to configure | Live owner attach |
|---|---|---|
| Same host as Hermes | usually defaults + local Hermes `.env` are enough | possible with compatible owner seam |
| Another VM/host | `--api-url` + bridge-side key/env file; Hermes must be reachable over LAN/VPN/tunnel | not through the owner lease |
| Launched on Hermes host through SSH | remote MCP client uses SSH; bridge still uses local Hermes config/secrets | recommended remote shape for experimental live attach |

For the default same-host case:

~~~bash
hermes config set API_SERVER_ENABLED true
hermes config set API_SERVER_KEY '<strong-secret>'
hermes gateway restart

hermes-control-mcp doctor
~~~

A healthy durable-only deployment may report the live tier as unavailable and
still return `READY`.

If Hermes is on another machine, **do not expect the zero-argument doctor to
discover it**: the default API target is `http://127.0.0.1:8642`. Use
`--api-url` and make the key available to the bridge, for example:

~~~bash
hermes-control-mcp doctor \
  --api-url http://192.168.1.50:8642 \
  --env-file ~/.config/hermes-control-mcp/hermes.env
~~~

For named profiles, SSH deployments, LAN exposure, state DB ownership,
`API_SERVER_KEY` creation/resolution, platform limits, and doctor
troubleshooting, read the **[Getting started and connection topologies](docs/GETTING-STARTED.md)** guide.

### MCP host configuration

The smallest configuration below is valid only when the bridge runs in an
environment where its defaults are correct (normally the same host/user as
Hermes):

~~~json
{
  "mcpServers": {
    "hermes": {
      "command": "hermes-control-mcp"
    }
  }
}
~~~

MCP client syntax varies. Some hosts require `"type": "stdio"`; that field is
client configuration, not a bridge option.

Examples:

- [generic local stdio](examples/mcp-stdio.json)
- [ZCode → remote Hermes API](examples/zcode-remote-api.json)
- [ZCode → SSH-launched bridge on Hermes host](examples/zcode-ssh.json)
- [ZCode → SSH + experimental live owner attach](examples/zcode-ssh-live.json)

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

CI tests Python 3.11, 3.12, 3.13, and 3.14. It also builds wheel + sdist and performs clean installed-wheel MCP smokes on the lowest and highest supported interpreters (3.11 and 3.14).

## Documentation

- [Getting started and connection topologies](docs/GETTING-STARTED.md)
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
