# Security Policy

## Supported versions

Until a stable release exists, security fixes are provided for the latest published public beta only.

## Reporting a vulnerability

Please **do not** open a public issue for vulnerabilities.

Use GitHub **Private vulnerability reporting** for this repository.

High-value reports include:

- API key, owner credential, refresh token, or other secret disclosure;
- cross-profile routing or credential-boundary bypass;
- request/idempotency confusion that can duplicate a mutation;
- live-session attribution that can return another writer's completion;
- owner/native attach admission bypass;
- path traversal through profile or state configuration;
- arbitrary gateway method exposure outside the documented MCP allowlist;
- registry behavior that persists raw prompts, credentials, or unsafe response bodies.

Please include:

- affected version/commit;
- deployment topology (durable only vs live attach, default vs multiplex profiles);
- minimal reproduction steps;
- expected vs observed security boundary;
- logs or traces with secrets removed.

## Security model notes

Hermes MCP Control Plane is a client/control plane, not a second Hermes runtime.

- Hermes remains the authority for agent execution, sessions, providers, tools, and approvals.
- Named profiles are treated as separate routing/credential scopes.
- The bridge does not expose arbitrary shell execution, raw gateway RPC, slash commands, or Hermes credential/config mutation.
- The public beta advertises one bridge process per state DB.
- Shared live attach is experimental and depends on a compatible Hermes owner/native attach seam.

Security-sensitive upstream changes in Hermes may require a bridge compatibility patch even when the MCP-facing surface remains unchanged.
