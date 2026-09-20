# UX1 implementation brief — client-config onboarding

Status: **PROPOSED IMPLEMENTATION CONTRACT**

Planning base:

~~~text
30f3fa1d5b6319133ece77b8c002e6c58ab59092
~~~

Start implementation from fresh `main` and record the actual base SHA in the
implementation PR.

## Objective

Reduce the public-beta onboarding path to:

~~~text
install -> doctor -> generate client config -> connect
~~~

without changing the bridge trust model.

UX1 is not a new transport and not an installer wizard. It is a small,
deterministic configuration-generation layer over the existing stdio bridge,
same-host discovery, and SSH remote pattern.

The public invariant after this patch is:

~~~text
client-config output is derived from one typed deployment plan
~~~

not:

~~~text
each client renderer independently guesses bridge/Hermes topology
~~~

The command must remain safe to run repeatedly: no Hermes config mutation, no
third-party client config mutation, no secret material in generated output.

## Accepted product decisions

### Command surface

Add one new CLI command:

~~~text
hermes-control-mcp client-config <client>
~~~

Supported client IDs for UX1:

~~~text
zcode
claude-code
cursor
codex
vscode
~~~

Remote stdio generation:

~~~text
hermes-control-mcp client-config <client> --ssh <host>
~~~

Optional naming:

~~~text
--name <server-name>
~~~

Default MCP server name:

~~~text
hermes
~~~

The exact argparse implementation may preserve the current positional-command
parser or migrate to subparsers, but all existing public invocations must stay
compatible:

~~~text
hermes-control-mcp
hermes-control-mcp doctor
hermes-control-mcp doctor --json
hermes-control-mcp --api-url ...
~~~

Do not make a parser cleanup a hidden requirement of this feature.

### Output contract

Default stdout contains **only the generated config payload**, suitable for
copy/paste or shell redirection.

Examples:

- JSON for ZCode / Claude Code / Cursor / VS Code;
- TOML for Codex.

Human guidance, warnings, discovered destination paths, and preflight notes go
to stderr.

Do not wrap stdout in Markdown fences.

Exit codes:

- `0`: config generated successfully;
- `2`: invalid CLI/config/discovery input;
- non-zero SSH/preflight failures must be translated to concise actionable
  diagnostics, not raw tracebacks.

### No implicit writes

UX1 never edits:

- `~/.zcode/cli/config.json`;
- `~/.agents/mcp.json`;
- `~/.cursor/mcp.json`;
- `~/.codex/config.toml`;
- VS Code configuration;
- Hermes config or profile env files.

Explicit client-file mutation belongs to a later UX milestone, if ever.

Redirection remains a user-owned action:

~~~bash
hermes-control-mcp client-config codex > /tmp/hermes-codex.toml
~~~

### Same-host plan

Same-host generation assumes the bridge will run under the same user as Hermes.

The generated command should prefer the actual installed
`hermes-control-mcp` executable when discoverable. Do not assume that GUI
clients inherit the same PATH as the interactive shell.

Preferred command discovery order:

1. explicit future/internal override supplied by tests/caller;
2. `shutil.which("hermes-control-mcp")`;
3. fallback to literal `hermes-control-mcp` with a stderr warning.

Do not embed Python virtualenv internals unless that path is the actual
installed console entrypoint discovered by `which`.

No `--api-url`, `--env-file`, or `--profiles-root` flags belong in the
normal same-host output.

### State DB policy

Generated configs should use an explicit per-client state DB to avoid the
public-beta one-process-per-DB collision when multiple MCP hosts may run
concurrently.

Local default:

~~~text
~/.local/state/hermes-control-mcp/clients/<client>-<server-name>.db
~~~

The emitted argument must be an absolute path resolved on the machine that will
run the bridge. Do not rely on `~`, shell expansion, or client-specific env
interpolation inside stdio args.

Sanitize `client` and `server-name` before constructing a path. Reject names
that cannot be represented safely rather than silently rewriting them into
ambiguous identities.

The generator must not create the DB as a side effect. The bridge creates/owns
it later when launched.

### Client-specific formats

Render from one normalized stdio server plan:

~~~text
name
command
args[]
optional env{}
transport = stdio
~~~

Client renderers only translate that plan into host syntax.

Required outputs:

#### ZCode

Use ZCode's native user-config shape:

~~~json
{
  "mcp": {
    "servers": {
      "hermes": {
        "command": "...",
        "args": []
      }
    }
  }
}
~~~

Do not emit `mcpServers` as ZCode's canonical default in this command.

#### Claude Code

Emit project-compatible `.mcp.json` shape:

~~~json
{
  "mcpServers": {
    "hermes": {
      "command": "...",
      "args": []
    }
  }
}
~~~

Do not shell out to `claude mcp add` in UX1.

#### Cursor

Emit:

~~~json
{
  "mcpServers": {
    "hermes": {
      "type": "stdio",
      "command": "...",
      "args": []
    }
  }
}
~~~

#### Codex

Emit TOML:

~~~toml
[mcp_servers.hermes]
command = "..."
args = []
~~~

Do not add remote HTTP/OAuth fields in this milestone.

#### VS Code

Emit:

~~~json
{
  "servers": {
    "hermes": {
      "type": "stdio",
      "command": "...",
      "args": []
    }
  }
}
~~~

### Remote SSH plan

`--ssh <host>` means:

~~~text
local MCP client -> local ssh process -> remote hermes-control-mcp stdio
~~~

The bridge itself runs on the Hermes host. Hermes API/profile credentials remain
remote and local to Hermes.

UX1 must not generate the direct-remote-API / mirrored-secret topology.

The SSH argv begins with:

~~~text
ssh -T <host>
~~~

and then launches the remote bridge with an explicit remote executable and
remote absolute state DB path.

### SSH discovery

A useful `--ssh` mode requires discovering two remote values:

1. absolute `hermes-control-mcp` executable;
2. remote home directory for the explicit per-client state DB.

Use one bounded, read-only SSH preflight. It may execute a fixed remote shell
snippet equivalent to:

~~~sh
command -v hermes-control-mcp
printf '%s\n' "$HOME"
~~~

Requirements:

- invoke local `ssh` with argv, never `shell=True`;
- host is passed as one argv element, not interpolated into a local shell;
- the remote probe command is static and does not include user-controlled
  fragments;
- timeout is bounded;
- no file writes;
- no credential values returned or logged;
- reject empty/non-absolute remote executable/home results;
- preserve ordinary SSH host verification and authentication behavior;
- translate failures into actionable stderr text.

Do **not** parse or mutate `~/.ssh/config` yourself. Let OpenSSH resolve aliases,
ProxyJump, ports, identity files, Tailscale hostnames, etc.

### Remote readiness

The SSH discovery step is transport/setup discovery, not a second doctor
implementation.

Do not duplicate API/profile readiness checks inside the renderer.

After discovery, stderr should recommend:

~~~text
ssh <host> '<absolute-bridge> doctor'
~~~

or equivalent guidance when readiness has not been established.

A future `--check` flag may run remote doctor, but it is not required for UX1.

### Secret boundary

Generated output must never contain:

- `API_SERVER_KEY`;
- named-profile keys;
- bearer tokens;
- dashboard/live credentials;
- raw `.env` contents;
- credentials copied into `env`, argv, URLs, or comments.

Same-host and SSH generation should need no credential values.

If the current environment contains secret canaries, output and errors must
remain clean.

## Internal architecture

### Plan first, render second

Recommended module:

~~~text
src/hermes_control_mcp/client_config.py
~~~

Suggested model:

~~~python
@dataclass(frozen=True, slots=True)
class StdioServerPlan:
    name: str
    command: str
    args: tuple[str, ...]
~~~

Optional supporting model:

~~~python
@dataclass(frozen=True, slots=True)
class SSHDiscovery:
    host: str
    remote_command: str
    remote_home: str
~~~

The exact names are not contractual. The separation is.

Pipeline:

~~~text
CLI args
  -> validate client/name/mode
  -> discover local or SSH execution facts
  -> construct one StdioServerPlan
  -> render for target client
  -> stdout payload
~~~

Client renderers must not perform filesystem/network discovery.

Discovery helpers must not know JSON/TOML host schemas.

### Reuse doctor/config discovery

Do not copy the API-key/source/path logic from `doctor.py`.

Where UX1 needs effective local configuration metadata, extract or expose a
small secret-free helper that both doctor and onboarding can reuse.

Acceptable examples:

~~~text
describe_effective_config(config)
bridge_environment(config)
~~~

Avoid a broad diagnostics framework refactor. UX1 only needs enough shared
logic to keep config resolution rules single-sourced.

### JSON rendering

Use the stdlib `json` module with deterministic indentation.

No hand-built JSON strings.

### TOML rendering

Do not add a TOML-writing dependency only for this tiny deterministic payload.

The Codex renderer may emit the small fixed TOML shape directly, but it must
correctly quote/escape names, command paths, and args according to TOML basic
string rules.

If the implementation cannot make that quoting obviously auditable, add a
small local escaping helper with adversarial tests rather than a new package.

## CLI help and user guidance

`hermes-control-mcp --help` and `client-config --help` must make these points
obvious:

- config generation is non-mutating;
- same-host is the simplest path;
- `--ssh` keeps the bridge and Hermes secrets on the remote Hermes host;
- remote HTTP MCP is not implemented by this command.

After successful generation, stderr may state the usual destination, e.g.:

~~~text
ZCode native user config: ~/.zcode/cli/config.json
Codex user config: ~/.codex/config.toml
~~~

but must not claim the file was written.

## Scope A — local client-config

Implement and test local generation for all five client IDs.

Required behavior:

1. actual bridge executable is preferred when discoverable;
2. one explicit absolute per-client state DB is included;
3. no unnecessary Hermes routing flags are emitted;
4. output is deterministic;
5. secret-free;
6. no files are written.

## Scope B — SSH client-config

Implement and test `--ssh` for all five clients.

Required behavior:

1. local command is `ssh`;
2. `-T` is present;
3. remote bridge executable comes from bounded SSH discovery;
4. remote state DB uses the discovered remote home and client/name identity;
5. no Hermes profile/API credentials cross hosts;
6. SSH aliases are accepted unchanged as OpenSSH argv;
7. discovery failure produces actionable diagnostics and no partial config on
   stdout.

## Scope C — docs-as-contract

Update:

- `README.md`;
- `docs/GETTING-STARTED.md`;
- `docs/MCP-CLIENTS.md`;
- `CHANGELOG.md`.

The command output becomes the canonical source for client examples.

At minimum, docs should show:

~~~text
hermes-control-mcp doctor
hermes-control-mcp client-config zcode
hermes-control-mcp client-config codex --ssh hermes-host
~~~

Do not maintain a second independently invented set of argument layouts in docs.

If practical, add checked example fixtures under `examples/` generated from
the same renderer calls used by tests. A dedicated code-generation framework is
not required.

## Scope D — installed-package smoke

Extend clean-wheel smoke coverage so an installed package can:

1. invoke `client-config zcode`;
2. invoke `client-config codex`;
3. produce parseable/non-empty output without access to repository source.

Do not require a real SSH endpoint in package CI.

## Required tests

### CLI compatibility

1. bare `hermes-control-mcp` still means `serve`;
2. existing serve flags remain accepted;
3. `doctor` commands/flags remain accepted;
4. unknown client fails clearly;
5. missing client after `client-config` fails clearly.

### Local plan

6. local executable discovery prefers an absolute installed command;
7. fallback command emits a warning but still generates;
8. local state DB is absolute and client/name-specific;
9. generator does not create the state DB;
10. unsafe server name is rejected.

### Renderers

11. ZCode uses native `mcp.servers`;
12. Claude Code uses `mcpServers`;
13. Cursor uses `mcpServers` + `type=stdio`;
14. Codex emits valid expected TOML;
15. VS Code uses top-level `servers`;
16. command/arg strings with spaces, quotes and backslashes are escaped safely;
17. repeated rendering is byte-deterministic.

### SSH

18. SSH uses argv execution, never local `shell=True`;
19. discovery returns absolute remote command/home;
20. SSH config contains `ssh -T <host> ...`;
21. remote state DB is absolute and client/name-specific;
22. host aliases are not reinterpreted or shell-expanded by the bridge;
23. timeout/auth/host-key failures produce no config stdout;
24. malicious-looking host text cannot become local shell syntax.

### Secret boundary

25. default API key canary never appears;
26. named profile key canary never appears;
27. env/live credential canaries never appear;
28. stderr diagnostics remain secret-free.

### Side-effect boundary

29. no client configuration file is created/modified;
30. no Hermes configuration file is created/modified;
31. no API run/prompt is submitted.

### Installed artifact

32. installed wheel can generate ZCode JSON;
33. installed wheel can generate Codex TOML.

All existing doctor/durable/live tests remain green.

## Acceptance smoke

### Same host

On a machine where Hermes and the bridge are already healthy:

~~~bash
hermes-control-mcp doctor
hermes-control-mcp client-config zcode > /tmp/zcode.json
hermes-control-mcp client-config codex > /tmp/codex.toml
~~~

Verify:

- output parses;
- generated command starts the installed bridge;
- generated DB path is private bridge state, not a Hermes DB;
- no secret value appears.

### SSH

Against a safe Hermes host alias:

~~~bash
hermes-control-mcp client-config zcode --ssh hermes-host
~~~

Verify:

- normal SSH trust/auth flow is preserved;
- remote bridge path/home are discovered;
- generated config starts remote stdio successfully;
- profile secrets remain only on the Hermes host;
- a named-profile durable call works after the generated config is installed
  manually in the client.

Do not report this manual smoke as CI.

## Non-goals

UX1 does not:

- implement Streamable HTTP MCP;
- expose Hermes API port publicly;
- generate direct-remote-API configs with copied `.env` secrets;
- write/merge third-party client config files;
- install or upgrade Hermes;
- install or upgrade `hermes-control-mcp` on a remote host;
- create SSH keys/config entries;
- bypass host-key verification;
- add OAuth/bearer auth;
- implement a GUI/TUI setup wizard;
- change durable/live request semantics;
- change profile routing/security;
- migrate to Agent Sessions;
- implement approvals/clarify;
- publish a release.

## Likely owner files

Expected:

~~~text
src/hermes_control_mcp/client_config.py
src/hermes_control_mcp/server.py
src/hermes_control_mcp/doctor.py        # only if extracting shared secret-free discovery
tests/test_client_config.py
tests/test_doctor.py                    # only if shared helper moves
scripts/installed_smoke.py
README.md
docs/GETTING-STARTED.md
docs/MCP-CLIENTS.md
CHANGELOG.md
~~~

A small additional helper module is acceptable when it cleanly separates
discovery from rendering.

Avoid changes to durable service, registry schema, live protocol, MCP tool
surface, or Hermes upstream seams.

## Evidence contract

Before review:

~~~bash
./scripts/test.sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
python3 -m py_compile src/hermes_control_mcp/*.py
git diff --check
~~~

The implementation PR receipt must record:

- exact base/head SHA;
- CLI compatibility results;
- generated local config fixture for all five clients;
- generated SSH config fixture for all five clients;
- SSH discovery test strategy;
- secret-canary results;
- no-write/side-effect results;
- installed-wheel smoke result;
- full test count;
- no release/deploy/restart unless separately authorized.

## Adversarial review focus

High-value attacks:

1. client renderer leaks an API key through `env` or args;
2. local config works in an interactive shell but GUI client cannot find the
   bridge because PATH was assumed;
3. two clients accidentally share one state DB;
4. remote generator falls back to copying profile keys instead of co-locating
   the bridge;
5. SSH host value reaches `shell=True` or command interpolation;
6. remote executable/home discovery accepts malformed relative output;
7. Codex TOML quoting breaks on spaces/quotes/backslashes;
8. ZCode renderer emits the compatibility `mcpServers` shape while claiming it
   is the native default;
9. generator writes a third-party config file without explicit user action;
10. parser refactor breaks existing bare serve or doctor usage;
11. docs drift away from actual generated argv;
12. secret canary appears in stdout/stderr/tests.

Review outcome:

~~~text
PASS-TO-MERGE | REMEDIATE | BLOCKED-FOR-MAINTAINER
~~~

Default workflow remains one implementation pass, one adversarial review, and
remediation only for concrete findings.
