#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from device_identity import resolve_client_id


class DeviceIdentityTests(unittest.TestCase):
    def test_shared_packaged_identity_is_migrated_to_source_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity_path = root / "device_id"
            shared_config = root / "dps_config.json"
            shared_config.write_text(
                json.dumps({"client_id": "b" * 32}), encoding="utf-8"
            )
            source_config = {"client_id": "a" * 32}

            resolved = resolve_client_id(
                source_config, identity_path, shared_config
            )

            self.assertEqual(resolved, "b" * 32)
            self.assertEqual(source_config["client_id"], "b" * 32)
            self.assertEqual(identity_path.read_text(encoding="ascii").strip(), "b" * 32)

    def test_persisted_identity_wins_over_all_configs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity_path = root / "device_id"
            identity_path.write_text("c" * 32, encoding="ascii")
            shared_config = root / "dps_config.json"
            shared_config.write_text(
                json.dumps({"client_id": "b" * 32}), encoding="utf-8"
            )
            source_config = {"client_id": "a" * 32}

            resolved = resolve_client_id(
                source_config, identity_path, shared_config
            )

            self.assertEqual(resolved, "c" * 32)
            self.assertEqual(source_config["client_id"], "c" * 32)

    def test_invalid_values_create_and_reuse_a_valid_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity_path = root / "device_id"
            config = {"client_id": "not-valid"}

            first = resolve_client_id(config, identity_path, root / "missing.json")
            second = resolve_client_id({}, identity_path, root / "missing.json")

            self.assertRegex(first, r"^[0-9a-f]{32}$")
            self.assertEqual(second, first)


if __name__ == "__main__":
    unittest.main()
