from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from hermes_control_mcp import client_config as cc
from hermes_control_mcp.config import state_home
from hermes_control_mcp.server import build_parser, main


CANARY = "ux1-canary-secret-DO-NOT-EMIT"
CANARY_ENV = {
    "API_SERVER_KEY": CANARY,
    "HERMES_DASHBOARD_SESSION_TOKEN": CANARY,
    "HERMES_DASHBOARD_ACCESS_TOKEN": CANARY,
    "HERMES_DASHBOARD_REFRESH_TOKEN": CANARY,
    "HERMES_CODER_KEY": CANARY,
}

ABSOLUTE_BRIDGE = "/opt/hermes-tools/bin/hermes-control-mcp"
FIXTURE_COMMAND = "/home/user/.local/bin/hermes-control-mcp"
FIXTURE_HOME = "/home/user"
REPO_ROOT = Path(__file__).parents[1]


def run_main(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class FakeSSHRunner:
    """Strict two-argument ssh runner stand-in.

    The signature is deliberately narrow: if the bridge ever invoked ssh with
    ``shell=True`` or extra keyword arguments, discovery would fail here.
    """

    def __init__(self, *, stdout: str = "", returncode: int = 0, error: Exception | None = None):
        self.stdout = stdout
        self.returncode = returncode
        self.error = error
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), timeout))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr="")


def json_server_entry(payload_text: str, client: str, name: str = "hermes") -> dict:
    payload = json.loads(payload_text)
    if client == "zcode":
        return payload["mcp"]["servers"][name]
    if client == "vscode":
        return payload["servers"][name]
    return payload["mcpServers"][name]


class ClientConfigCliCompatTests(unittest.TestCase):
    def test_bare_invocation_still_defaults_to_serve(self):
        args = build_parser().parse_args([])
        self.assertEqual(args.command, "serve")
        self.assertIsNone(args.client)

    def test_existing_serve_flags_remain_accepted(self):
        args = build_parser().parse_args([
            "serve",
            "--api-url", "http://127.0.0.1:9999",
            "--state-db", "/tmp/bridge.db",
            "--api-key-env", "SOME_KEY_ENV",
            "--timeout", "5",
            "--poll-interval", "0.2",
            "--log-level", "INFO",
            "--gateway-url", "ws://127.0.0.1:9119/api/ws",
        ])
        self.assertEqual(args.command, "serve")
        self.assertEqual(args.api_url, "http://127.0.0.1:9999")

    def test_doctor_flags_remain_accepted(self):
        args = build_parser().parse_args([
            "doctor", "--json", "--profile", "coder", "--all-profiles", "--require-live",
        ])
        self.assertEqual(args.command, "doctor")
        self.assertTrue(args.json)
        self.assertEqual(args.profile, ["coder"])

    def test_unknown_client_fails_clearly_without_output(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                main(["client-config", "emacs"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("invalid choice", err.getvalue())

    def test_missing_client_after_client_config_fails_clearly(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                main(["client-config"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("requires a target client", err.getvalue())

    def test_client_positional_rejected_for_other_commands(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                main(["doctor", "zcode"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("unrecognized arguments", err.getvalue())

    def test_client_config_only_flags_rejected_for_other_commands(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                main(["serve", "--name", "hermes"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("only valid with the client-config command", err.getvalue())

    def test_cli_explicit_empty_name_is_rejected_not_rewritten_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp, "XDG_STATE_HOME": str(Path(tmp) / "state"), "PATH": "/usr/bin:/bin"}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(cc.shutil, "which", return_value=ABSOLUTE_BRIDGE):
                for argv in (["client-config", "zcode", "--name", ""], ["client-config", "zcode", "--name="]):
                    with self.subTest(argv=argv):
                        code, out, err = run_main(argv)
                        self.assertEqual(code, 2)
                        self.assertEqual(out, "")
                        self.assertIn("Invalid MCP server name", err)

    def test_malformed_xdg_state_home_falls_back_gracefully(self):
        # A malformed ~user XDG value is treated as unset: generation must not
        # traceback (exit 1) and must stay consistent with where the bridge
        # itself would resolve its state home under the same environment.
        env = {
            "HOME": "/home/ubuntu",
            "XDG_STATE_HOME": "~definitely-no-such-user-ux1/state",
            "PATH": "/usr/bin:/bin",
        }
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(cc.shutil, "which", return_value=ABSOLUTE_BRIDGE):
            code, out, err = run_main(["client-config", "zcode"])
        self.assertEqual(code, 0)
        self.assertNotIn("Traceback", err)
        payload = json.loads(out)
        db = payload["mcp"]["servers"]["hermes"]["args"][1]
        self.assertEqual(
            db, "/home/ubuntu/.local/state/hermes-control-mcp/clients/zcode-hermes.db"
        )


class ClientConfigLocalPlanTests(unittest.TestCase):
    def test_local_discovery_prefers_absolute_installed_command(self):
        plan = cc.build_local_plan("zcode", "hermes", which=lambda _name: ABSOLUTE_BRIDGE)
        self.assertEqual(plan.command, ABSOLUTE_BRIDGE)
        self.assertTrue(Path(plan.command).is_absolute())

    def test_explicit_executable_override_wins_over_discovery(self):
        plan = cc.build_local_plan(
            "zcode", "hermes",
            executable_override="/custom/path/bridge",
            which=lambda _name: ABSOLUTE_BRIDGE,
        )
        self.assertEqual(plan.command, "/custom/path/bridge")

    def test_relative_which_result_is_normalized_to_absolute(self):
        # Adversarial control for the real shutil.which: a cwd-relative PATH
        # element makes which() return "./hermes-control-mcp", which must not
        # reach the generated config as a GUI-host command.
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / cc.BRIDGE_COMMAND
            exe.write_text("#!/bin/sh\nexit 0\n")
            exe.chmod(0o755)
            previous_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                env = dict(os.environ)
                env["PATH"] = "."
                warnings: list[str] = []
                with mock.patch.dict(os.environ, env, clear=True):
                    plan = cc.build_local_plan("zcode", "hermes", warn=warnings.append)
            finally:
                os.chdir(previous_cwd)
            self.assertTrue(Path(plan.command).is_absolute())
            self.assertEqual(Path(plan.command), Path(tmp) / cc.BRIDGE_COMMAND)
            self.assertEqual(warnings, [])

    def test_fallback_command_warns_but_still_generates(self):
        warnings: list[str] = []
        plan = cc.build_local_plan("zcode", "hermes", which=lambda _name: None, warn=warnings.append)
        self.assertEqual(plan.command, cc.BRIDGE_COMMAND)
        self.assertEqual(len(warnings), 1)
        self.assertIn("PATH", warnings[0])

    def test_local_state_db_is_absolute_and_client_name_specific(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/ux1-state"}):
            zcode = cc.client_state_db("zcode", "hermes")
            codex = cc.client_state_db("codex", "second-agent")
        self.assertTrue(zcode.is_absolute())
        self.assertTrue(codex.is_absolute())
        self.assertNotEqual(zcode, codex)
        self.assertEqual(zcode.name, "zcode-hermes.db")
        self.assertEqual(codex.name, "codex-second-agent.db")
        self.assertIn(str(Path("hermes-control-mcp") / "clients"), str(zcode))

    def test_relative_xdg_state_home_falls_back_to_absolute_default(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "relative/state"}):
            self.assertTrue(state_home().is_absolute())
            self.assertTrue(cc.client_state_db("zcode", "hermes").is_absolute())

    def test_generator_does_not_create_the_state_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp, "HOME": tmp}):
                plan = cc.build_local_plan("zcode", "hermes", which=lambda _name: ABSOLUTE_BRIDGE)
                cc.render_plan(plan, "zcode")
            self.assertFalse(Path(plan.args[-1]).exists())

    def test_unsafe_server_name_is_rejected(self):
        for bad in ("", "a b", "../evil", "-lead", "a" * 65, "hermes;rm", "a.b", "hermes\n", "таб"):
            with self.subTest(name=bad):
                with self.assertRaises(cc.ClientConfigError):
                    cc.validate_server_name(bad)
                with self.assertRaises(cc.ClientConfigError):
                    cc.build_local_plan("zcode", bad)

    def test_documented_server_name_charset_is_accepted(self):
        self.assertEqual(cc.validate_server_name("hermes"), "hermes")
        self.assertEqual(cc.validate_server_name("Agent_2"), "Agent_2")
        self.assertEqual(cc.validate_server_name("a" * 64), "a" * 64)

    def test_unsupported_client_is_rejected(self):
        with self.assertRaises(cc.ClientConfigError):
            cc.build_local_plan("emacs", "hermes")
        with self.assertRaises(cc.ClientConfigError):
            cc.render_plan(cc.StdioServerPlan(name="hermes", command="x", args=()), "emacs")


class ClientConfigRendererTests(unittest.TestCase):
    def make_plan(self, client: str = "zcode", name: str = "hermes") -> cc.StdioServerPlan:
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/ux1-state"}):
            return cc.build_local_plan(client, name, which=lambda _name: ABSOLUTE_BRIDGE)

    def test_zcode_uses_native_mcp_servers(self):
        text = cc.render_plan(self.make_plan(), "zcode")
        payload = json.loads(text)
        self.assertEqual(set(payload), {"mcp"})
        server = payload["mcp"]["servers"]["hermes"]
        self.assertEqual(server["command"], ABSOLUTE_BRIDGE)
        self.assertNotIn("mcpServers", text)
        self.assertNotIn("type", server)

    def test_claude_code_uses_mcpServers(self):
        payload = json.loads(cc.render_plan(self.make_plan(), "claude-code"))
        self.assertEqual(set(payload), {"mcpServers"})
        server = payload["mcpServers"]["hermes"]
        self.assertNotIn("type", server)

    def test_cursor_uses_mcpServers_with_stdio_type(self):
        payload = json.loads(cc.render_plan(self.make_plan(), "cursor"))
        self.assertEqual(set(payload), {"mcpServers"})
        self.assertEqual(payload["mcpServers"]["hermes"]["type"], "stdio")

    def test_codex_emits_expected_toml(self):
        text = cc.render_plan(self.make_plan("codex"), "codex")
        payload = tomllib.loads(text)
        self.assertEqual(payload, {
            "mcp_servers": {
                "hermes": {
                    "command": ABSOLUTE_BRIDGE,
                    "args": ["--state-db", "/tmp/ux1-state/hermes-control-mcp/clients/codex-hermes.db"],
                },
            },
        })

    def test_codex_renders_empty_args_list(self):
        text = cc._render_codex_toml(cc.StdioServerPlan(name="hermes", command="/bin/bridge", args=()))
        self.assertIn("args = []", text)
        self.assertEqual(tomllib.loads(text)["mcp_servers"]["hermes"]["args"], [])

    def test_vscode_uses_top_level_servers(self):
        payload = json.loads(cc.render_plan(self.make_plan(), "vscode"))
        self.assertEqual(set(payload), {"servers"})
        self.assertEqual(payload["servers"]["hermes"]["type"], "stdio")

    def test_command_and_args_with_spaces_quotes_backslashes_roundtrip(self):
        tricky_command = '/opt/we ird/bin/bridge "quoted"\\back\\slash\ttab'
        tricky_db = 'C:\\dir "x"\\file.db'
        plan = cc.StdioServerPlan(name="hermes", command=tricky_command, args=("--state-db", tricky_db))
        for client in ("zcode", "claude-code", "cursor", "vscode"):
            entry = json_server_entry(cc.render_plan(plan, client), client)
            self.assertEqual(entry["command"], tricky_command)
            self.assertEqual(entry["args"], ["--state-db", tricky_db])
        codex = tomllib.loads(cc.render_plan(plan, "codex"))["mcp_servers"]["hermes"]
        self.assertEqual(codex["command"], tricky_command)
        self.assertEqual(codex["args"], ["--state-db", tricky_db])

    def test_toml_basic_string_escapes_control_characters(self):
        self.assertEqual(cc.toml_basic_string('a"b\\c'), '"a\\"b\\\\c"')
        self.assertEqual(cc.toml_basic_string("\x00\x1f\x7f"), '"\\u0000\\u001F\\u007F"')
        unicode_value = "значение ✓"
        roundtripped = tomllib.loads(f"x = {cc.toml_basic_string(unicode_value)}")["x"]
        self.assertEqual(roundtripped, unicode_value)

    def test_repeated_rendering_is_byte_deterministic(self):
        for client in cc.SUPPORTED_CLIENTS:
            with self.subTest(client=client):
                first = cc.render_plan(self.make_plan(client), client)
                second = cc.render_plan(self.make_plan(client), client)
                self.assertEqual(first, second)


class ClientConfigSSHTests(unittest.TestCase):
    DISCOVERY_OUTPUT = "/opt/hermes/bin/hermes-control-mcp\n/home/hermes\n"

    def make_runner(self, **kwargs) -> FakeSSHRunner:
        return FakeSSHRunner(stdout=self.DISCOVERY_OUTPUT, **kwargs)

    def test_ssh_uses_argv_execution_without_shell(self):
        runner = self.make_runner()
        cc.discover_ssh("hermes-host", runner=runner)
        argv, timeout = runner.calls[0]
        self.assertEqual(argv, ["ssh", "-T", "hermes-host", cc.SSH_PROBE_COMMAND])
        self.assertGreater(timeout, 0)

    def test_discovery_returns_absolute_remote_command_and_home(self):
        discovery = cc.discover_ssh("hermes-host", runner=self.make_runner())
        self.assertEqual(discovery.host, "hermes-host")
        self.assertEqual(discovery.remote_command, "/opt/hermes/bin/hermes-control-mcp")
        self.assertEqual(discovery.remote_home, "/home/hermes")

    def test_discovery_rejects_relative_remote_executable(self):
        runner = FakeSSHRunner(stdout="hermes-control-mcp\n/home/hermes\n")
        with self.assertRaises(cc.ClientConfigError) as ctx:
            cc.discover_ssh("hermes-host", runner=runner)
        self.assertIn("malformed remote executable", str(ctx.exception))

    def test_discovery_rejects_missing_remote_executable(self):
        runner = FakeSSHRunner(stdout="\n/home/hermes\n")
        with self.assertRaises(cc.ClientConfigError) as ctx:
            cc.discover_ssh("hermes-host", runner=runner)
        self.assertIn("not found in the remote PATH", str(ctx.exception))

    def test_discovery_rejects_malformed_remote_home(self):
        runner = FakeSSHRunner(stdout="/opt/x/bin/hermes-control-mcp\nrelative-home\n")
        with self.assertRaises(cc.ClientConfigError):
            cc.discover_ssh("hermes-host", runner=runner)

    def test_discovery_translates_nonzero_exit_without_stdout_config(self):
        runner = FakeSSHRunner(returncode=255)
        with self.assertRaises(cc.ClientConfigError) as ctx:
            cc.discover_ssh("hermes-host", runner=runner)
        self.assertIn("255", str(ctx.exception))
        self.assertIn("hermes-host", str(ctx.exception))

    def test_discovery_translates_timeout(self):
        runner = FakeSSHRunner(error=subprocess.TimeoutExpired(cmd="ssh", timeout=1))
        with self.assertRaises(cc.ClientConfigError) as ctx:
            cc.discover_ssh("hermes-host", runner=runner)
        self.assertIn("timed out", str(ctx.exception))

    def test_ssh_plan_contains_ssh_T_host_and_remote_executable(self):
        discovery = cc.discover_ssh("hermes-host", runner=self.make_runner())
        plan = cc.build_ssh_plan("zcode", "hermes", discovery)
        self.assertEqual(plan.command, "ssh")
        self.assertEqual(plan.args[0], "-T")
        self.assertEqual(plan.args[1], "hermes-host")
        remote_tokens = shlex.split(plan.args[2])
        self.assertEqual(remote_tokens[0], "/opt/hermes/bin/hermes-control-mcp")
        self.assertIn("--state-db", remote_tokens)

    def test_remote_state_db_is_absolute_and_client_name_specific(self):
        discovery = cc.discover_ssh("hermes-host", runner=self.make_runner())
        codex_plan = cc.build_ssh_plan("codex", "second-agent", discovery)
        zcode_plan = cc.build_ssh_plan("zcode", "hermes", discovery)
        tokens = shlex.split(codex_plan.args[2])
        db = tokens[tokens.index("--state-db") + 1]
        self.assertEqual(
            db,
            "/home/hermes/.local/state/hermes-control-mcp/clients/codex-second-agent.db",
        )
        self.assertNotEqual(zcode_plan.args[2], codex_plan.args[2])

    def test_remote_paths_with_spaces_survive_remote_shell_parsing(self):
        runner = FakeSSHRunner(stdout="/opt/hermes tools/bin/bridge\n/home/her mes\n")
        discovery = cc.discover_ssh("hermes-host", runner=runner)
        plan = cc.build_ssh_plan("zcode", "hermes", discovery)
        tokens = shlex.split(plan.args[2])
        self.assertEqual(tokens[0], "/opt/hermes tools/bin/bridge")
        db = tokens[tokens.index("--state-db") + 1]
        self.assertEqual(
            db, "/home/her mes/.local/state/hermes-control-mcp/clients/zcode-hermes.db"
        )

    def test_ssh_host_alias_is_passed_verbatim(self):
        runner = self.make_runner()
        cc.discover_ssh("my-tailscale-alias", runner=runner)
        self.assertEqual(runner.calls[0][0][2], "my-tailscale-alias")

    def test_option_or_shell_looking_hosts_are_rejected_before_any_ssh_call(self):
        runner = self.make_runner()
        for host in (
            "-oProxyCommand=evil",
            "h; rm -rf /tmp",
            "host $(calc)",
            "a b",
            "a\tb",
            "host\nnext",
            "",
        ):
            with self.subTest(host=host):
                with self.assertRaises(cc.ClientConfigError):
                    cc.discover_ssh(host, runner=runner)
        self.assertEqual(runner.calls, [])

    def test_cli_ssh_failure_produces_no_config_stdout(self):
        runner = FakeSSHRunner(returncode=255)
        original = cc.discover_ssh
        with mock.patch.object(cc, "discover_ssh", lambda host, **_kw: original(host, runner=runner)):
            code, out, err = run_main(["client-config", "zcode", "--ssh", "hermes-host"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("hermes-host", err)
        self.assertIn("255", err)

    def test_cli_malicious_host_cannot_reach_local_shell_syntax(self):
        code, out, err = run_main(["client-config", "zcode", "--ssh=-oProxyCommand=evil"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Invalid SSH host", err)


class ClientConfigSecretBoundaryTests(unittest.TestCase):
    def prepare_secretful_home(self) -> str:
        home = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        hermes_env = Path(home) / ".hermes" / ".env"
        hermes_env.parent.mkdir(parents=True)
        hermes_env.write_text(f"API_SERVER_KEY={CANARY}\n", encoding="utf-8")
        profile_env = Path(home) / ".hermes" / "profiles" / "coder" / ".env"
        profile_env.parent.mkdir(parents=True)
        profile_env.write_text(f"API_SERVER_KEY={CANARY}\n", encoding="utf-8")
        return home

    def test_local_generation_never_emits_secret_canaries(self):
        home = self.prepare_secretful_home()
        state = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(state, ignore_errors=True))
        env = dict(os.environ)
        env.update(CANARY_ENV)
        env["HOME"] = home
        env["XDG_STATE_HOME"] = state
        with mock.patch.dict(os.environ, env, clear=True):
            for client in cc.SUPPORTED_CLIENTS:
                plan = cc.build_local_plan(client, "hermes", which=lambda _name: ABSOLUTE_BRIDGE)
                payload = cc.render_plan(plan, client)
                self.assertNotIn(CANARY, payload)
                self.assertNotIn("API_SERVER_KEY", payload)
            code, out, err = run_main(["client-config", "cursor"])
        self.assertEqual(code, 0)
        self.assertNotIn(CANARY, out + err)

    def test_ssh_generation_never_emits_secret_canaries(self):
        with mock.patch.dict(os.environ, dict(os.environ, **CANARY_ENV)):
            runner = FakeSSHRunner(stdout="/opt/bridge\n/home/u\n")
            discovery = cc.discover_ssh("hermes-host", runner=runner)
            for client in cc.SUPPORTED_CLIENTS:
                plan = cc.build_ssh_plan(client, "hermes", discovery)
                self.assertNotIn(CANARY, cc.render_plan(plan, client))

    def test_cli_ssh_full_path_is_secret_free_on_stdout_and_stderr(self):
        home = self.prepare_secretful_home()
        state = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(state, ignore_errors=True))
        env = dict(os.environ)
        env.update(CANARY_ENV)
        env["HOME"] = home
        env["XDG_STATE_HOME"] = state
        original = cc.discover_ssh
        runner = FakeSSHRunner(stdout="/opt/bridge\n/home/u\n")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(cc, "discover_ssh", lambda host, **_kw: original(host, runner=runner)):
            code, out, err = run_main(["client-config", "zcode", "--ssh", "hermes-host"])
        self.assertEqual(code, 0)
        self.assertNotIn(CANARY, out)
        self.assertNotIn(CANARY, err)
        self.assertNotIn("API_SERVER_KEY", out)
        self.assertIn("--state-db", out)


class ClientConfigSideEffectTests(unittest.TestCase):
    def plant_config_files(self, home: str) -> dict[str, bytes]:
        planted = {
            ".zcode/cli/config.json": '{"mcp": {"servers": {}}}',
            ".agents/mcp.json": '{"mcpServers": {}}',
            ".cursor/mcp.json": '{"mcpServers": {}}',
            ".codex/config.toml": '[mcp_servers.other]\ncommand = "x"\n',
            ".vscode/mcp.json": '{"servers": {}}',
            ".hermes/.env": f"API_SERVER_KEY={CANARY}\n",
            ".hermes/config.json": "{}",
            ".hermes/profiles/coder/.env": f"API_SERVER_KEY={CANARY}\n",
        }
        for relative, content in planted.items():
            path = Path(home) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return planted

    def snapshot_home(self, home: str) -> dict[str, bytes]:
        return {
            str(path.relative_to(home)): path.read_bytes()
            for path in Path(home).rglob("*")
            if path.is_file()
        }

    def test_generation_writes_no_client_or_hermes_files(self):
        with tempfile.TemporaryDirectory() as home:
            self.plant_config_files(home)
            state = Path(home) / "state"
            env = dict(os.environ)
            env.update(CANARY_ENV)
            env["HOME"] = home
            env["XDG_STATE_HOME"] = str(state)
            before = self.snapshot_home(home)
            with mock.patch.dict(os.environ, env, clear=True):
                for argv in (["client-config", "zcode"], ["client-config", "codex"]):
                    code, out, err = run_main(argv)
                    self.assertEqual(code, 0)
            self.assertEqual(self.snapshot_home(home), before)
            # the per-client state DB is described but never created
            self.assertFalse((state / "hermes-control-mcp" / "clients" / "zcode-hermes.db").exists())

    def test_generation_needs_no_hermes_api_or_credentials(self):
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home, "XDG_STATE_HOME": str(Path(home) / "state"), "PATH": "/usr/bin:/bin"}
            with mock.patch.dict(os.environ, env, clear=True):
                code, out, err = run_main(["client-config", "zcode"])
            self.assertEqual(code, 0)
            json.loads(out)


class ClientConfigCliEndToEndTests(unittest.TestCase):
    def test_cli_generation_for_all_clients_is_parseable(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["HOME"] = tmp
            env["XDG_STATE_HOME"] = str(Path(tmp) / "state")
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(cc.shutil, "which", return_value=ABSOLUTE_BRIDGE):
                for client in cc.SUPPORTED_CLIENTS:
                    with self.subTest(client=client):
                        code, out, err = run_main(["client-config", client])
                        self.assertEqual(code, 0)
                        self.assertIn("--state-db", out)
                        if client == "codex":
                            tomllib.loads(out)
                        else:
                            json.loads(out)

    def test_cli_fallback_command_warns_on_stderr_but_generates(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["HOME"] = tmp
            env["XDG_STATE_HOME"] = str(Path(tmp) / "state")
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(cc.shutil, "which", return_value=None):
                code, out, err = run_main(["client-config", "zcode"])
            self.assertEqual(code, 0)
            self.assertIn('"command": "hermes-control-mcp"', out)
            self.assertIn("PATH", err)


class ClientConfigExampleFixtureTests(unittest.TestCase):
    def fixture_plan(self, client: str) -> cc.StdioServerPlan:
        return cc.StdioServerPlan(
            name="hermes",
            command=FIXTURE_COMMAND,
            args=("--state-db", f"{FIXTURE_HOME}/.local/state/hermes-control-mcp/clients/{client}-hermes.db"),
        )

    def test_checked_examples_match_renderers_exactly(self):
        for client in cc.SUPPORTED_CLIENTS:
            suffix = "toml" if client == "codex" else "json"
            path = REPO_ROOT / "examples" / "client-config" / f"{client}.{suffix}"
            with self.subTest(client=client):
                self.assertTrue(path.exists(), f"missing fixture {path}")
                self.assertEqual(cc.render_plan(self.fixture_plan(client), client), path.read_text(encoding="utf-8"))

    def test_zcode_ssh_example_matches_renderer(self):
        discovery = cc.SSHDiscovery(host="hermes-host", remote_command=FIXTURE_COMMAND, remote_home=FIXTURE_HOME)
        plan = cc.build_ssh_plan("zcode", "hermes", discovery)
        path = REPO_ROOT / "examples" / "zcode-ssh.json"
        self.assertEqual(cc.render_plan(plan, "zcode"), path.read_text(encoding="utf-8"))

    def test_generic_stdio_example_matches_renderer(self):
        plan = cc.StdioServerPlan(
            name="hermes",
            command=FIXTURE_COMMAND,
            args=("--state-db", f"{FIXTURE_HOME}/.local/state/hermes-control-mcp/clients/claude-code-hermes.db"),
        )
        path = REPO_ROOT / "examples" / "mcp-stdio.json"
        self.assertEqual(cc.render_plan(plan, "claude-code"), path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
