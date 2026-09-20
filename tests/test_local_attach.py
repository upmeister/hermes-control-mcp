from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from hermes_control_mcp.local_attach import OwnerAttachError, load_owner_attach_target, process_start_marker


class LocalAttachTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hzba-")
        self.runtime = Path(self.tmp.name) / "owner"
        self.runtime.mkdir(mode=0o700)
        self.lease = self.runtime / "owner_adapter.json"
        self.socket_path = self.runtime / "owner_adapter.sock"
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.socket_path))
        self.sock.listen(1)
        self.socket_path.chmod(0o600)
        self.profile = Path(self.tmp.name) / "profile"
        self.profile.mkdir(mode=0o700)
        self.write_lease()

    def tearDown(self):
        self.sock.close()
        self.socket_path.unlink(missing_ok=True)
        self.lease.unlink(missing_ok=True)
        self.tmp.cleanup()

    def write_lease(self, **overrides):
        payload = {
            "version": 1,
            "runtime_id": "runtime-1",
            "pid": os.getpid(),
            "process_start": process_start_marker(os.getpid()),
            "profile_home": str(self.profile),
            "socket_path": str(self.socket_path),
            "lease_path": str(self.lease),
            "route": "/api/owner/ws",
            "transport": "websocket-unix",
            "host": "127.0.0.1",
            "port": 49119,
        }
        payload.update(overrides)
        self.lease.write_text(json.dumps(payload), encoding="utf-8")
        self.lease.chmod(0o600)

    def test_valid_lease_builds_identity_only_uri_for_uds(self):
        target = load_owner_attach_target(self.lease)

        self.assertEqual(target.socket_path, self.socket_path.resolve())
        parts = urlsplit(target.uri)
        self.assertEqual(parts.scheme, "ws")
        self.assertEqual(parts.netloc, "127.0.0.1:49119")
        self.assertEqual(parts.path, "/api/owner/ws")
        query = parse_qs(parts.query)
        self.assertEqual(query["runtime_id"], ["runtime-1"])
        self.assertNotIn("token", query)
        self.assertNotIn("ticket", query)
        self.assertNotIn("internal", query)

    def test_broad_lease_permissions_are_rejected(self):
        self.lease.chmod(0o644)
        with self.assertRaisesRegex(OwnerAttachError, "private"):
            load_owner_attach_target(self.lease)

    def test_symlinked_lease_is_rejected(self):
        alias = self.runtime / "owner-alias.json"
        alias.symlink_to(self.lease)
        with self.assertRaisesRegex(OwnerAttachError, "symlink"):
            load_owner_attach_target(alias)

    def test_changed_socket_identity_or_endpoint_is_rejected(self):
        self.write_lease(socket_path="/tmp/not-the-owner.sock")
        with self.assertRaisesRegex(OwnerAttachError, "socket"):
            load_owner_attach_target(self.lease)

        self.write_lease(host="", port=0)
        with self.assertRaisesRegex(OwnerAttachError, "endpoint"):
            load_owner_attach_target(self.lease)

    def test_relative_lease_path_is_rejected(self):
        with self.assertRaisesRegex(OwnerAttachError, "absolute"):
            load_owner_attach_target(Path("owner_adapter.json"))

    def test_symlinked_lease_parent_is_rejected(self):
        real = Path(self.tmp.name) / "real-runtime"
        real.mkdir(mode=0o700)
        alias = Path(self.tmp.name) / "runtime-alias"
        alias.symlink_to(real, target_is_directory=True)
        lease = alias / "owner_adapter.json"
        lease.write_text(self.lease.read_text(encoding="utf-8"), encoding="utf-8")
        lease.chmod(0o600)

        with self.assertRaisesRegex(OwnerAttachError, "symlink"):
            load_owner_attach_target(lease)

    def test_process_start_marker_fences_pid_reuse(self):
        self.write_lease(process_start="proc:not-this-process")
        with self.assertRaisesRegex(OwnerAttachError, "process"):
            load_owner_attach_target(self.lease)


if __name__ == "__main__":
    unittest.main()
