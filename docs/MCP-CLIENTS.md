# MCP client configuration

MCP standardizes the protocol between a client and a server. It does **not**
standardize every host application's configuration file, top-level JSON key,
installation UI, or secret interpolation syntax.

Hermes MCP Control Plane currently serves MCP over **stdio**. The bridge command
therefore runs on the machine where the MCP host starts it. For a remote client,
the practical public-beta pattern is to make the stdio command be `ssh` and run
`hermes-control-mcp` on the Hermes host.

Remote Streamable HTTP MCP is a future transport boundary and is not implemented
in the current release.

## Generating the configuration with the bridge

Instead of maintaining per-client shapes by hand, let the bridge render the
right one:

~~~bash
hermes-control-mcp client-config zcode        # native ZCode mcp.servers shape
hermes-control-mcp client-config claude-code  # mcpServers / .mcp.json shape
hermes-control-mcp client-config cursor       # mcpServers with type = "stdio"
hermes-control-mcp client-config codex        # TOML for ~/.codex/config.toml
hermes-control-mcp client-config vscode       # top-level servers shape
~~~

Properties shared by every generated configuration:

- stdout carries only the generated payload; guidance and warnings go to stderr;
- the command is **non-mutating**: no client, bridge, or Hermes configuration
  file is created or edited — redirection remains a user-owned action;
- each config embeds an explicit **absolute per-client state DB**, so MCP hosts
  that may run concurrently never share one bridge registry;
- the actually installed `hermes-control-mcp` executable is preferred when
  discoverable, so GUI-launched hosts do not depend on your interactive shell
  PATH (a fallback emits the bare command name with a warning);
- `--name <server-name>` renames the MCP server entry (default: `hermes`);
- no Hermes routing flags (`--api-url`, `--env-file`, `--profiles-root`) and no
  credential values are emitted; keys stay in the bridge process environment.

The JSON/TOML payload examples in this document are the **exact output** of the
`client-config` renderers for an installation at
`/home/user/.local/bin/hermes-control-mcp`; the checked fixtures under
[`examples/client-config/`](../examples/client-config/) are byte-compared
against the same renderers in tests. Any hand-written minimal variant is
called out explicitly and is not generator output.

Repository example files:

- `examples/client-config/<client>.{json,toml}` — generated same-host fixtures;
- `examples/mcp-stdio.json` — generated fixture in the common `mcpServers`
  (Claude Code) shape;
- `examples/zcode-ssh.json` — generated `--ssh` fixture (native ZCode shape);
- `examples/zcode-ssh-live.json` — hand-written example for the experimental
  SSH + live owner-attach pattern; not generator output.

## Client matrix

| Client | Recommended setup surface | Local stdio shape | Remote HTTP support in client | Notes |
|---|---|---|---|---|
| ZCode | Settings -> MCP Servers, or import from another agent | native `mcp.servers`; Full configuration and `.agents/mcp.json` also accept `mcpServers` | Yes (HTTP/SSE, headers, OAuth) | `type` may be omitted when `command` implies stdio. ZCode can import Claude Code and Codex MCP configs. |
| Claude Code | `claude mcp add` / project `.mcp.json` | `mcpServers.<name>.command/args/env` | Yes (HTTP/SSE, OAuth) | Project-scoped `.mcp.json` is shareable; user scope is available through the CLI. |
| Cursor | Settings/Customize or `~/.cursor/mcp.json` / `.cursor/mcp.json` | `mcpServers.<name>.command/args/env` | Yes (URL, headers, OAuth) | Supports environment interpolation in command, args, env, URL and headers. |
| OpenAI Codex | Settings UI or `~/.codex/config.toml` | `[mcp_servers.<name>]` with `command` and optional `args` | Yes (Streamable HTTP, bearer/OAuth) | Desktop, CLI and IDE extension share Codex MCP configuration. |
| VS Code / Copilot | MCP: Add Server or `.vscode/mcp.json` | top-level `servers`; `type = "stdio"`, `command`, `args` | Yes (HTTP/SSE, headers, OAuth) | The top-level key is `servers`, not `mcpServers`. |

The JSON object used by Claude Code, Cursor and ZCode's compatibility/full-config
surfaces is a common **de-facto host format**, not an MCP protocol requirement.

## Same-host happy path

When the MCP client, `hermes-control-mcp`, and Hermes run on the same machine
under the same user, no bridge routing flags are normally required. The bridge
uses `http://127.0.0.1:8642`, reads the default Hermes key from
`~/.hermes/.env`, and resolves named-profile keys from
`~/.hermes/profiles/<profile>/.env`.

Before configuring a client:

~~~bash
hermes-control-mcp doctor
~~~

A ready durable core means the client entry can be minimal.

### ZCode

The easiest route is **Settings -> MCP Servers -> New MCP Server**:

- type: `stdio`
- command: `hermes-control-mcp` (the generator emits the discovered absolute path)
- no arguments are strictly required for a single-host, single-client setup;
  the generated config additionally embeds an explicit per-client state DB,
  which is recommended whenever more than one MCP host may run.

ZCode's native user configuration is stored under
`~/.zcode/cli/config.json`. Generated payload (`client-config zcode`):

~~~json
{
  "mcp": {
    "servers": {
      "hermes": {
        "command": "/home/user/.local/bin/hermes-control-mcp",
        "args": [
          "--state-db",
          "/home/user/.local/state/hermes-control-mcp/clients/zcode-hermes.db"
        ]
      }
    }
  }
}
~~~

ZCode also accepts the `mcpServers` form in Full configuration mode and in
`~/.agents/mcp.json`; use the Claude Code payload below for those surfaces.

### Claude Code

~~~bash
claude mcp add --scope user hermes -- hermes-control-mcp
~~~

For a project-scoped file, the generated payload (`client-config claude-code`):

~~~json
{
  "mcpServers": {
    "hermes": {
      "command": "/home/user/.local/bin/hermes-control-mcp",
      "args": [
        "--state-db",
        "/home/user/.local/state/hermes-control-mcp/clients/claude-code-hermes.db"
      ]
    }
  }
}
~~~

### Cursor

`~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project), generated
payload (`client-config cursor`):

~~~json
{
  "mcpServers": {
    "hermes": {
      "type": "stdio",
      "command": "/home/user/.local/bin/hermes-control-mcp",
      "args": [
        "--state-db",
        "/home/user/.local/state/hermes-control-mcp/clients/cursor-hermes.db"
      ]
    }
  }
}
~~~

### OpenAI Codex

`~/.codex/config.toml`, generated payload (`client-config codex`):

~~~toml
[mcp_servers.hermes]
command = "/home/user/.local/bin/hermes-control-mcp"
args = ["--state-db", "/home/user/.local/state/hermes-control-mcp/clients/codex-hermes.db"]
~~~

The Codex desktop/IDE settings UI can add the same server by choosing
**STDIO** and entering the bridge command as shown above.

### VS Code / Copilot

`.vscode/mcp.json` or the user MCP configuration, generated payload
(`client-config vscode`):

~~~json
{
  "servers": {
    "hermes": {
      "type": "stdio",
      "command": "/home/user/.local/bin/hermes-control-mcp",
      "args": [
        "--state-db",
        "/home/user/.local/state/hermes-control-mcp/clients/vscode-hermes.db"
      ]
    }
  }
}
~~~

## Remote client without copying Hermes secrets

For multi-profile deployments, prefer keeping the bridge next to Hermes and
making the MCP host launch it through SSH. The Hermes API keys and profile
`.env` files never need to be copied to the client machine.

The bridge automates this with one bounded read-only SSH preflight:

~~~bash
hermes-control-mcp client-config zcode --ssh hermes-host
~~~

`--ssh <host>` discovers the remote `hermes-control-mcp` executable and the
remote home directory, then emits a config whose stdio command is
`ssh -T <host> ...` launching the remote bridge with an explicit remote state
DB. SSH aliases, ProxyJump, ports, and identity files keep being resolved by
OpenSSH itself — the bridge never parses `~/.ssh/config`. When readiness has
not been established yet, the command's stderr suggests the matching
`ssh <host> '<bridge> doctor'` check.

The steps below describe what the command does and remain useful for manual
setups. First make sure the remote executable path is stable:

~~~bash
ssh hermes-host 'command -v hermes-control-mcp'
~~~

Then use that absolute path in the client command — or let the generator do
all of it, including the per-client state DB name.

### Claude Code, Cursor, and ZCode compatibility surfaces

Generated payload of `client-config claude-code --ssh hermes-host` (Cursor
adds `"type": "stdio"`, ZCode Full configuration / `.agents/mcp.json` accept
the same shape; each client's own DB name comes from running the command for
that client):

~~~json
{
  "mcpServers": {
    "hermes": {
      "command": "ssh",
      "args": [
        "-T",
        "hermes-host",
        "/home/user/.local/bin/hermes-control-mcp --state-db /home/user/.local/state/hermes-control-mcp/clients/claude-code-hermes.db"
      ]
    }
  }
}
~~~

Note that the remote bridge command and its `--state-db` argument travel as
**one ssh argv element**; the remote shell parses it, so discovered paths with
spaces stay safe.

### Codex

Generated payload of `client-config codex --ssh hermes-host`:

~~~toml
[mcp_servers.hermes]
command = "ssh"
args = ["-T", "hermes-host", "/home/user/.local/bin/hermes-control-mcp --state-db /home/user/.local/state/hermes-control-mcp/clients/codex-hermes.db"]
~~~

### VS Code

Generated payload of `client-config vscode --ssh hermes-host`:

~~~json
{
  "servers": {
    "hermes": {
      "type": "stdio",
      "command": "ssh",
      "args": [
        "-T",
        "hermes-host",
        "/home/user/.local/bin/hermes-control-mcp --state-db /home/user/.local/state/hermes-control-mcp/clients/vscode-hermes.db"
      ]
    }
  }
}
~~~

Use one state DB per bridge process that may be alive concurrently.

## Why direct remote API mode is not the preferred multi-profile UX

A bridge running on a different VM can point directly at Hermes with
`--api-url`. The default profile key can be supplied through a bridge-side env
file. Named profiles are different: the current bridge deliberately resolves
each profile's own key from
`<profiles-root>/<profile>/.env`.

Mirroring that entire secret tree onto a second machine works, but it creates a
credential-distribution problem that the same-host and SSH shapes avoid.

The longer-term remote UX should therefore keep the trust boundary near Hermes:
a native authenticated Streamable HTTP transport for Hermes MCP Control Plane
can expose the MCP surface remotely while profile secrets stay local to the
Hermes host. That is a separate security/runtime milestone, not a documentation
shortcut for the current stdio release.

## Official client references

- ZCode: <https://zcode.z.ai/en/docs/mcp-services>
- Claude Code: <https://docs.anthropic.com/en/docs/claude-code/mcp>
- Cursor: <https://cursor.com/docs/mcp>
- OpenAI Codex: <https://developers.openai.com/docs/extend/mcp>
- VS Code: <https://code.visualstudio.com/docs/agents/reference/mcp-configuration>
