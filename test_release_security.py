#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import release_security
from release_security import (
    ReleaseIdentity,
    load_release_identity,
    verify_release_resources,
    verify_windows_publisher,
)


BUILD_ID = "a" * 32
PUBLISHER = "B" * 40
CAPABILITY_KEY_ID = "lease-2026-09"
CAPABILITY_PUBLIC_KEY = base64.b64encode(b"K" * 32).decode("ascii")


class ReleaseSecurityTests(unittest.TestCase):
    def test_build_embeds_development_profile_only_for_unprotected_builds(self):
        build_script = Path(__file__).with_name("build_exe.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn('if (-not $ProtectedRelease) {', build_script)
        self.assertIn(
            '"--include-data-files=$DevelopmentRuntimeProfilePath=runtime-profile.dev.json"',
            build_script,
        )
        self.assertIn(
            '"--include-data-files=assets/professions/*.png=assets/professions/"',
            build_script,
        )
        self.assertIn(
            '"--include-data-files=assets/skills/*.png=assets/skills/"',
            build_script,
        )
        self.assertIn(
            '"--include-data-files=assets/bosses/*.png=assets/bosses/"',
            build_script,
        )
        self.assertIn(
            '"--include-data-files=$SanitizedBossCatalogPath=assets/bosses/boss_icon_sources.json"',
            build_script,
        )
        self.assertNotIn(
            '"--include-data-files=assets/**/*.png=assets/"',
            build_script,
        )
        self.assertIn(
            "Protected builds receive\n# the same data only through short-lived, server-signed runtime capabilities",
            build_script,
        )
        self.assertIn("CapabilitySigningKeyId", build_script)
        self.assertIn("CapabilityPublicKey", build_script)

    def test_missing_identity_is_explicitly_development_only(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = load_release_identity(Path(directory))

        self.assertFalse(identity.official)
        self.assertFalse(identity.valid_official)
        self.assertFalse(identity.valid_protected)
        self.assertEqual(identity.build_id, "source-development")

    def test_official_identity_verifies_inventory_and_detects_modification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resource = root / "skill_names.json"
            resource.write_text('{"1":"skill"}', encoding="utf-8")
            digest = hashlib.sha256(resource.read_bytes()).hexdigest()
            (root / release_security.RELEASE_IDENTITY_FILENAME).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "build_id": BUILD_ID,
                        "version": "0.1.2",
                        "client_build": "0.1.2+20260903.1",
                        "official": True,
                        "publisher_thumbprints": [PUBLISHER],
                        "resource_hashes": {"skill_names.json": digest},
                    }
                ),
                encoding="utf-8",
            )

            identity = load_release_identity(root)
            self.assertTrue(identity.valid_official)
            self.assertEqual(verify_release_resources(identity, root), (True, ""))

            resource.write_text('{"1":"modified"}', encoding="utf-8")
            verified, message = verify_release_resources(identity, root)

        self.assertFalse(verified)
        self.assertIn("modified", message)

    def test_official_identity_rejects_resource_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / release_security.RELEASE_IDENTITY_FILENAME).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "build_id": BUILD_ID,
                        "version": "0.1.2",
                        "client_build": "0.1.2+20260902.1",
                        "official": True,
                        "publisher_thumbprints": [PUBLISHER],
                        "resource_hashes": {"../secret": "c" * 64},
                    }
                ),
                encoding="utf-8",
            )

            identity = load_release_identity(root)

        self.assertFalse(identity.valid_official)
        self.assertIn("invalid", identity.error)

    def test_protected_unsigned_identity_loads_trusted_capability_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resource = root / "skill_names.json"
            resource.write_text('{"1":"skill"}', encoding="utf-8")
            digest = hashlib.sha256(resource.read_bytes()).hexdigest()
            (root / release_security.RELEASE_IDENTITY_FILENAME).write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "build_id": BUILD_ID,
                        "version": "0.1.7",
                        "client_build": "0.1.7+20260905.1",
                        "official": False,
                        "protected": True,
                        "publisher_thumbprints": [],
                        "capability_public_keys": {
                            CAPABILITY_KEY_ID: CAPABILITY_PUBLIC_KEY
                        },
                        "resource_hashes": {"skill_names.json": digest},
                    }
                ),
                encoding="utf-8",
            )

            identity = load_release_identity(root)

        self.assertFalse(identity.valid_official)
        self.assertTrue(identity.valid_protected)
        self.assertEqual(
            identity.capability_public_keys[CAPABILITY_KEY_ID],
            CAPABILITY_PUBLIC_KEY,
        )

    def test_protected_identity_rejects_missing_or_invalid_public_key(self):
        for public_keys in ({}, {CAPABILITY_KEY_ID: "not-base64"}):
            with self.subTest(public_keys=public_keys):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / release_security.RELEASE_IDENTITY_FILENAME).write_text(
                        json.dumps(
                            {
                                "schema_version": 2,
                                "build_id": BUILD_ID,
                                "version": "0.1.7",
                                "client_build": "0.1.7+20260905.1",
                                "protected": True,
                                "capability_public_keys": public_keys,
                                "resource_hashes": {},
                            }
                        ),
                        encoding="utf-8",
                    )
                    identity = load_release_identity(root)

                self.assertFalse(identity.valid_protected)
                self.assertTrue(identity.error)

    def test_publisher_verification_requires_valid_chain_and_pinned_signer(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "client.exe"
            target.write_bytes(b"MZ")
            powershell = Path(directory) / "powershell.exe"
            powershell.write_bytes(b"stub")
            result = subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(
                    {
                        "Status": "Valid",
                        "StatusMessage": "Signature verified.",
                        "Thumbprint": PUBLISHER,
                        "Subject": "CN=Dps-Logs",
                    }
                ),
                stderr="",
            )
            with (
                mock.patch.object(release_security.sys, "platform", "win32"),
                mock.patch.object(
                    release_security, "_windows_powershell_path", return_value=powershell
                ),
                mock.patch.object(release_security.subprocess, "run", return_value=result),
            ):
                verified = verify_windows_publisher(target, [PUBLISHER])
                rejected = verify_windows_publisher(target, ["C" * 40])

        self.assertTrue(verified.verified)
        self.assertEqual(verified.thumbprint, PUBLISHER)
        self.assertFalse(rejected.verified)
        self.assertIn("not trusted", rejected.message)

    def test_untrusted_authenticode_chain_is_rejected_even_with_matching_pin(self):
        identity = ReleaseIdentity(
            build_id=BUILD_ID,
            version="0.1.2",
            client_build="0.1.2+20260903.1",
            official=True,
            publisher_thumbprints=(PUBLISHER,),
        )
        self.assertTrue(identity.valid_official)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "client.exe"
            target.write_bytes(b"MZ")
            powershell = Path(directory) / "powershell.exe"
            powershell.write_bytes(b"stub")
            result = subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(
                    {
                        "Status": "UnknownError",
                        "StatusMessage": "A certificate chain could not be built.",
                        "Thumbprint": PUBLISHER,
                        "Subject": "CN=Dps-Logs",
                    }
                ),
                stderr="",
            )
            with (
                mock.patch.object(release_security.sys, "platform", "win32"),
                mock.patch.object(
                    release_security, "_windows_powershell_path", return_value=powershell
                ),
                mock.patch.object(release_security.subprocess, "run", return_value=result),
            ):
                verification = verify_windows_publisher(target, [PUBLISHER])

        self.assertFalse(verification.verified)
        self.assertEqual(verification.status, "UnknownError")


if __name__ == "__main__":
    unittest.main()
