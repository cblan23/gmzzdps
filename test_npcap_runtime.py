#!/usr/bin/env python3

import tempfile
import unittest
from pathlib import Path

from npcap_runtime import (
    NpcapRuntimeInfo,
    require_npcap_runtime,
    windows_file_version,
)


class NpcapRuntimeTests(unittest.TestCase):
    def test_formats_driver_version(self):
        info = NpcapRuntimeInfo(Path("npcap.sys"), (1, 88, 0, 0))
        self.assertEqual(info.version, "1.88")

    def test_accepts_required_or_newer_version(self):
        with tempfile.TemporaryDirectory() as directory:
            driver = Path(directory) / "npcap.sys"
            driver.touch()
            info = require_npcap_runtime(
                driver_path=driver,
                version_reader=lambda _path: (1, 88, 0, 0),
            )
        self.assertEqual(info.version, "1.88")

    def test_rejects_old_version_with_user_facing_message(self):
        with tempfile.TemporaryDirectory() as directory:
            driver = Path(directory) / "npcap.sys"
            driver.touch()
            with self.assertRaisesRegex(RuntimeError, "请升级到 1.88"):
                require_npcap_runtime(
                    driver_path=driver,
                    version_reader=lambda _path: (1, 79, 0, 0),
                )

    def test_rejects_missing_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            driver = Path(directory) / "npcap.sys"
            with self.assertRaisesRegex(RuntimeError, "未检测到 Npcap 1.88"):
                require_npcap_runtime(driver_path=driver)

    def test_installed_driver_reports_public_product_version(self):
        path = Path(r"C:\Windows\System32\drivers\npcap.sys")
        if not path.is_file():
            self.skipTest("Npcap is not installed")
        self.assertEqual(windows_file_version(path)[:2], (1, 88))


if __name__ == "__main__":
    unittest.main()
