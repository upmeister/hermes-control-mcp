# Getting started and connection topologies

This guide starts from a fresh `hermes-control-mcp` installation and explains
what must be configured on the Hermes side, how authentication works, and how
to choose a bridge topology.

## The two processes

Hermes MCP Control Plane does not start Hermes itself.

~~~text
MCP host (ZCode, Claude Desktop, etc.)
        | stdio
        v
hermes-control-mcp
        | HTTP (durable)
        v
Hermes API Server

optional:
hermes-control-mcp
        | local owner attach
        v
existing Hermes TUI/Desktop runtime
~~~

For the stable durable tier, the bridge needs:

1. a reachable Hermes API Server URL;
2. the matching `API_SERVER_KEY`;
3. a writable local state database.

Shared live attach is optional and has additional same-host requirements.

If everything runs on one machine under one user, start with that path first.
It is intentionally the simplest deployment: the default API URL, default key,
and named-profile key tree can all be discovered locally.

Client configuration files are a separate concern. MCP defines the wire
protocol, not one universal host-config schema. See
[MCP client configuration](MCP-CLIENTS.md) for ZCode, Claude Code, Cursor,
Codex, and VS Code examples.

## What is `API_SERVER_KEY`?

`API_SERVER_KEY` is the bearer credential that protects Hermes' API Server. It
is **not** an LLM/provider key. A caller that knows it can authenticate to the
Hermes agent API, including endpoints that can execute agent turns and tools,
so treat it as a real server credential.

Hermes stores the default profile's key in:

~~~text
~/.hermes/.env
~~~

A named profile such as `coder` has its own independent key in:

~~~text
~/.hermes/profiles/coder/.env
~~~

Under a multiplexed Hermes gateway, `/p/coder/...` requests authenticate with
`coder`'s own key. The default key is rejected. Hermes MCP Control Plane
preserves that boundary and never borrows the default key for a named profile.

### Create or replace the default profile key

Generate a strong random value:

~~~bash
openssl rand -hex 32
~~~

Then configure Hermes with that value:

~~~bash
hermes config set API_SERVER_ENABLED true
hermes config set API_SERVER_KEY '<generated-value>'
~~~

`hermes config set` routes the secret into `~/.hermes/.env`.

If you already have an `API_SERVER_KEY`, do not rotate it casually: existing
API clients will stop authenticating until they are updated.

### Create a key for a named profile

With a multiplexed default gateway:

~~~bash
hermes -p coder config set API_SERVER_KEY '<different-generated-value>'
hermes gateway restart
~~~

Do **not** enable a second API listener for the secondary profile when it is
served by the default multiplexer. The shared listener serves it through
`/p/coder/...`; the profile's key is only its authentication boundary.

If multiplexing is not already active and you deliberately want the default
gateway to serve named profiles:

~~~bash
hermes config set gateway.multiplex_profiles true
hermes gateway restart
~~~

Read the [multiplexing caveat](COMPATIBILITY.md#hermes-multiplexing-caveat)
before forcing this on an old installation with copied messaging credentials.

## How the bridge resolves configuration

Hermes MCP Control Plane currently has no persistent bridge-specific YAML/TOML
configuration file. Runtime configuration comes from CLI flags, environment
variables, and (when local to Hermes) Hermes' `.env` files.

### Durable API URL

Default:

~~~text
http://127.0.0.1:8642
~~~

Override it with:

~~~bash
hermes-control-mcp --api-url http://192.168.1.50:8642
~~~

### Default profile API key

The CLI resolves the default key in this order:

1. process environment variable named by `--api-key-env` (default:
   `API_SERVER_KEY`);
2. `--env-file`, if supplied;
3. `$HERMES_HOME/.env` (normally `~/.hermes/.env`).

Example with a dedicated bridge-side env file:

~~~bash
mkdir -p ~/.config/hermes-control-mcp
chmod 700 ~/.config/hermes-control-mcp
printf '%s\n' 'API_SERVER_KEY=<secret>' > ~/.config/hermes-control-mcp/hermes.env
chmod 600 ~/.config/hermes-control-mcp/hermes.env

hermes-control-mcp doctor \
  --api-url http://192.168.1.50:8642 \
  --env-file ~/.config/hermes-control-mcp/hermes.env
~~~

Do not put the key itself in MCP `args`; process lists and client logs can expose
command-line arguments.

### Named profile keys

Named profile keys are intentionally stricter. The bridge reads:

~~~text
<profiles-root>/<profile>/.env
~~~

where `--profiles-root` defaults to:

~~~text
$HERMES_HOME/profiles
~~~

This is why same-host/SSH execution is the simplest multi-profile deployment:
the bridge can read exactly the same profile secret files as Hermes.

If the bridge runs on a different machine, named-profile routing currently
requires a deliberately maintained local `--profiles-root` containing the
corresponding profile `.env` files. Copying credentials between hosts is often
less attractive than running the bridge on the Hermes host over SSH.

## What is `--state-db`?

`--state-db` is **not** a Hermes database. It is the bridge's private SQLite
registry for request identity, idempotency, lane/session bindings, profile
routing metadata, and recovery state.

Default:

~~~text
~/.local/state/hermes-control-mcp/bridge.db
~~~

Most single-client installations can omit the flag.

Use an explicit path when you want predictable per-client isolation, for
example:

~~~text
~/.local/state/hermes-control-mcp/zcode.db
~~~

The public-beta ownership contract is one bridge process per state DB. If two
independent MCP hosts may run bridge processes concurrently, give them separate
DB files.

## Choose a topology

| Topology | Durable API | Multi-profile | Shared live attach | Best for |
|---|---|---|---|---|
| Bridge and Hermes on same host | Yes | Yes, keys auto-discovered | Yes, with compatible owner seam | simplest local/server install |
| Bridge on another machine, direct LAN/VPN HTTP | Yes | Awkward: profile secret files must also exist bridge-side | No owner-lease attach | durable-only remote clients |
| MCP host launches bridge on Hermes host through SSH | Yes | Yes | Yes, with compatible owner seam | remote MCP clients, especially multi-profile/live |

### Topology A — bridge and Hermes on the same host

On the Hermes host:

~~~bash
hermes config set API_SERVER_ENABLED true
hermes config set API_SERVER_KEY '<strong-secret>'
hermes gateway restart

hermes-control-mcp doctor
~~~

Because the bridge runs under the same user, it can read `~/.hermes/.env` and
the default URL `http://127.0.0.1:8642` normally needs no override.

At this point the bridge itself needs no additional routing flags. Configure
your MCP host to launch `hermes-control-mcp` over stdio.

Do not treat one JSON snippet as universal client syntax. ZCode, Claude Code,
Cursor, Codex, and VS Code use related but different configuration surfaces;
the exact forms are collected in [MCP client configuration](MCP-CLIENTS.md).

### Topology B — bridge on another VM/host over the LAN or VPN

First expose the Hermes API Server to the trusted network. On the Hermes host:

~~~bash
hermes config set API_SERVER_ENABLED true
hermes config set API_SERVER_KEY '<strong-secret>'
hermes config set API_SERVER_HOST 0.0.0.0
hermes gateway restart
~~~

**Security:** the Hermes API can run powerful tools on the Hermes host. Do not
expose port 8642 directly to the public Internet. Restrict it with a host
firewall/security group/VPN to the intended client address(es), or use SSH
forwarding instead.

Verify from the bridge machine:

~~~bash
curl http://192.168.1.50:8642/health
curl -H 'Authorization: Bearer <secret>' http://192.168.1.50:8642/v1/models
~~~

Then test the bridge:

~~~bash
hermes-control-mcp doctor \
  --api-url http://192.168.1.50:8642 \
  --env-file ~/.config/hermes-control-mcp/hermes.env
~~~

A ZCode-style stdio config can look like:

~~~json
{
  "mcpServers": {
    "hermes": {
      "type": "stdio",
      "command": "hermes-control-mcp",
      "args": [
        "--api-url", "http://192.168.1.50:8642",
        "--env-file", "/home/user/.config/hermes-control-mcp/hermes.env",
        "--state-db", "/home/user/.local/state/hermes-control-mcp/zcode.db"
      ]
    }
  }
}
~~~

The state DB flag is optional; it is shown here to make client ownership
explicit.

### Topology C — remote MCP client, bridge launched on the Hermes host via SSH

This is the recommended remote topology when you need named profiles or the
experimental live tier. API/profile credentials stay on the Hermes host.

Install `hermes-control-mcp` on the Hermes host, then find its absolute path:

~~~bash
command -v hermes-control-mcp
~~~

Use that path in the remote MCP client's SSH command. Example:

~~~json
{
  "mcpServers": {
    "hermes": {
      "type": "stdio",
      "command": "ssh",
      "args": [
        "-T",
        "hermes-host",
        "/home/user/.local/bin/hermes-control-mcp",
        "--state-db",
        "/home/user/.local/state/hermes-control-mcp/zcode.db",
        "--log-level",
        "WARNING"
      ]
    }
  }
}
~~~

No `--api-url` or API-key argument is required in the common same-host case:
the remote bridge process talks to `127.0.0.1:8642` and reads Hermes' local
secret files.

#### Add experimental live owner attach

Only if your Hermes deployment includes the compatible owner-adapter seam:

~~~json
{
  "mcpServers": {
    "hermes": {
      "type": "stdio",
      "command": "ssh",
      "args": [
        "-T",
        "hermes-host",
        "/home/user/.local/bin/hermes-control-mcp",
        "--gateway-owner-lease",
        "/home/user/.hermes/runtime/owner_adapter/owner_adapter.json",
        "--state-db",
        "/home/user/.local/state/hermes-control-mcp/zcode-live.db",
        "--log-level",
        "WARNING"
      ]
    }
  }
}
~~~

The owner lease path is local to the machine running the bridge.

Current owner attach validates a same-UID Linux process through `/proc`, a
private lease, and a Unix-domain socket. Therefore the bridge process must run
on the same Linux host and as the same Unix user as the compatible Hermes owner
runtime. A bridge running on another VM cannot consume this lease over the
network.

## Platform support

Current release CI runs on Linux.

| Platform | Durable HTTP tier | Owner-lease live tier |
|---|---|---|
| Linux | Tested/supported | Experimental, supported with compatible owner seam |
| macOS | No deliberate durable-tier Linux dependency, but not CI-verified | Not supported by current owner attach |
| Windows | No deliberate durable-tier Linux dependency, but not CI-verified | Not supported by current owner attach |

The Linux dependency of owner attach is concrete: its identity fence currently
uses `/proc/<pid>/stat` and Unix-domain socket ownership checks.

## Reading `doctor` output

The normal human-readable report now includes the effective API URL, whether a
default API key was found and where it was resolved from, the profiles root,
the state DB, failure detail, and actionable next steps. Credential **values**
are never printed.

Use:

~~~bash
hermes-control-mcp doctor --json
~~~

when you want the same readiness data as structured JSON for automation or bug
reports.

Common results:

### `default API ... FAIL (transport_unknown)`

The bridge could not complete the HTTP probe to its configured API target.

Check, in order:

1. Did you mean the default `http://127.0.0.1:8642`, or do you need
   `--api-url http://<hermes-host>:8642`?
2. Is `API_SERVER_ENABLED=true` on Hermes?
3. Did you restart the Hermes gateway after changing API Server settings?
4. For a remote bridge, is Hermes bound beyond loopback (`API_SERVER_HOST`) and
   is port 8642 allowed by the firewall/VPN?
5. Can `curl <api-url>/health` reach it from the bridge machine?

### Authentication failure / `401`

The network path works, but the supplied `API_SERVER_KEY` does not match the
profile URL being called.

For the default profile, verify the bridge process environment / `--env-file`
and Hermes `~/.hermes/.env`.

For `/p/<profile>/...`, verify that profile's own `.env` key. The default key
must not authenticate another profile.

### `profile_key_unavailable`

The bridge was asked to probe a named profile but cannot find
`<profiles-root>/<profile>/.env` with an `API_SERVER_KEY`.

On a remote bridge host this commonly means the profile secret files exist only
on the Hermes machine. Prefer the SSH topology for multi-profile use.

### `live_not_configured`

This is normal for durable-only installations. Live attach is optional in the
public beta.

### `owner_attach_failed`

The configured owner lease is stale, unavailable, wrong-owner, or points at a
runtime that no longer matches its process/socket identity. Owner attach also
requires the bridge to run on the same Linux host/user.

## Useful CLI flags

| Flag | Meaning | Typical use |
|---|---|---|
| `--api-url URL` | Hermes API Server base URL | bridge is not on Hermes host |
| `--api-key-env NAME` | env variable holding default API key | custom process secret name |
| `--env-file PATH` | dotenv file used for bridge credentials | remote bridge / service wrapper |
| `--profiles-root PATH` | local root containing named profile `.env` files | non-default Hermes home / advanced remote setup |
| `--state-db PATH` | bridge private SQLite registry | per-client isolation |
| `--gateway-owner-lease PATH` | same-host private live attach lease | experimental live tier |
| `--log-level LEVEL` | bridge logs on stderr | diagnostics |

See `hermes-control-mcp --help` for the complete list.

## Next step

Once `doctor` reports the durable core as ready, configure your MCP host and
call `bridge_health` before submitting the first run.
