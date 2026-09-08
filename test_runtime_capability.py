#!/usr/bin/env python3

from __future__ import annotations

import copy
import queue
import time
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from capture_process import CaptureProcessClient, RuntimeLeaseGate
from runtime_capability import (
    RuntimeCapability,
    RuntimeCapabilityError,
    RuntimeCapabilityTimeError,
    capability_public_key_to_base64,
    create_development_capability,
    create_server_capability,
    load_runtime_profile_file,
    runtime_profile_digest,
)


PROFILE_PATH = Path(__file__).resolve().with_name("runtime-profile.dev.json")
SESSION_ID = "1" * 32
CLIENT_ID = "2" * 32
BUILD_ID = "a" * 32
CLIENT_BUILD = "0.1.7+20260905.1"
SIGNING_KEY_ID = "lease-2026-09"


class RuntimeCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = load_runtime_profile_file(PROFILE_PATH)
        self.private_key = Ed25519PrivateKey.generate()
        self.trusted_public_keys = {
            SIGNING_KEY_ID: capability_public_key_to_base64(
                self.private_key.public_key()
            )
        }

    def server_capability(
        self,
        *,
        now: float | None = None,
        lease_sequence: int = 1,
        client_id: str = CLIENT_ID,
        build_id: str = BUILD_ID,
    ) -> RuntimeCapability:
        return create_server_capability(
            self.profile,
            session_id=SESSION_ID,
            client_id=client_id,
            build_id=build_id,
            client_build=CLIENT_BUILD,
            lease_sequence=lease_sequence,
            signing_key_id=SIGNING_KEY_ID,
            signing_private_key=self.private_key,
            lease_seconds=120,
            now=now,
        )

    def test_capability_is_bound_to_session_build_and_profile_digest(self):
        capability = self.server_capability()

        parsed = RuntimeCapability.from_value(
            capability.to_wire(),
            expected_session_id=SESSION_ID,
            expected_client_id=CLIENT_ID,
            expected_build_id=BUILD_ID,
            expected_client_build=CLIENT_BUILD,
            trusted_public_keys=self.trusted_public_keys,
        )

        self.assertTrue(parsed.active)
        self.assertEqual(parsed.profile_id, "c7-2026-09-02")
        with self.assertRaisesRegex(RuntimeCapabilityError, "another session"):
            RuntimeCapability.from_value(
                capability,
                expected_session_id="2" * 32,
                trusted_public_keys=self.trusted_public_keys,
            )

    def test_profile_tampering_is_rejected_before_capture_can_start(self):
        value = self.server_capability().to_wire()
        tampered = copy.deepcopy(value)
        tampered["profile"]["hooks"]["damage"]["rva"] += 16

        with self.assertRaisesRegex(RuntimeCapabilityError, "digest"):
            CaptureProcessClient(
                runtime_capability=tampered,
                trusted_public_keys=self.trusted_public_keys,
            )

        tampered["profile_digest"] = runtime_profile_digest(tampered["profile"])
        with self.assertRaisesRegex(RuntimeCapabilityError, "signature"):
            RuntimeCapability.from_value(
                tampered,
                trusted_public_keys=self.trusted_public_keys,
            )

    def test_expired_capability_is_rejected(self):
        value = self.server_capability().to_wire()
        value["issued_at"] = time.time() - 180
        value["expires_at"] = time.time() - 60

        with self.assertRaisesRegex(RuntimeCapabilityTimeError, "expired"):
            RuntimeCapability.from_value(
                value,
                trusted_public_keys=self.trusted_public_keys,
            )

    def test_signed_capability_rejects_wrong_device_build_and_key(self):
        capability = self.server_capability()
        with self.assertRaisesRegex(RuntimeCapabilityError, "another client"):
            RuntimeCapability.from_value(
                capability,
                expected_client_id="3" * 32,
                trusted_public_keys=self.trusted_public_keys,
            )
        with self.assertRaisesRegex(RuntimeCapabilityError, "another build"):
            RuntimeCapability.from_value(
                capability,
                expected_build_id="b" * 32,
                trusted_public_keys=self.trusted_public_keys,
            )
        with self.assertRaisesRegex(RuntimeCapabilityError, "not trusted"):
            RuntimeCapability.from_value(capability, trusted_public_keys={})

        other_key = Ed25519PrivateKey.generate()
        with self.assertRaisesRegex(RuntimeCapabilityError, "signature"):
            RuntimeCapability.from_value(
                capability,
                trusted_public_keys={
                    SIGNING_KEY_ID: capability_public_key_to_base64(
                        other_key.public_key()
                    )
                },
            )

    def test_unsigned_server_capability_is_rejected(self):
        value = self.server_capability().to_wire()
        value["signature"] = ""
        with self.assertRaisesRegex(RuntimeCapabilityError, "signature"):
            RuntimeCapability.from_value(
                value,
                trusted_public_keys=self.trusted_public_keys,
            )

    def test_development_profile_requires_explicit_opt_in(self):
        capability = create_development_capability(
            PROFILE_PATH,
            session_id=SESSION_ID,
            client_id=CLIENT_ID,
            build_id="source-development",
            client_build=CLIENT_BUILD,
        )

        with self.assertRaisesRegex(RuntimeCapabilityError, "not allowed"):
            CaptureProcessClient(runtime_capability=capability)
        client = CaptureProcessClient(
            runtime_capability=capability,
            allow_development=True,
        )
        self.assertEqual(
            client.runtime_capability.profile_id,
            "c7-2026-09-02",
        )
        client.close()

    def test_lease_refresh_accepts_only_same_runtime_binding(self):
        first = self.server_capability(now=time.time())
        refreshed = self.server_capability(
            now=first.issued_at + 1,
            lease_sequence=2,
        )
        client = CaptureProcessClient(
            runtime_capability=first,
            trusted_public_keys=self.trusted_public_keys,
        )

        expires_at = client.refresh_runtime_capability(refreshed)

        self.assertEqual(expires_at, refreshed.expires_at)
        other = create_server_capability(
            self.profile,
            session_id="3" * 32,
            client_id=CLIENT_ID,
            build_id=BUILD_ID,
            client_build=CLIENT_BUILD,
            lease_sequence=3,
            signing_key_id=SIGNING_KEY_ID,
            signing_private_key=self.private_key,
        )
        with self.assertRaisesRegex(RuntimeCapabilityError, "another session"):
            client.refresh_runtime_capability(other)
        client.close()

    def test_lease_refresh_rejects_replay_and_closes_gate(self):
        first = self.server_capability()
        client = CaptureProcessClient(
            runtime_capability=first,
            trusted_public_keys=self.trusted_public_keys,
        )

        with self.assertRaisesRegex(RuntimeCapabilityError, "replayed"):
            client.refresh_runtime_capability(first)

        self.assertEqual(client.runtime_expiry_value.value, 0.0)
        self.assertTrue(client.stop_event.is_set())
        client.close()

    def test_child_lease_gate_independently_verifies_each_renewal(self):
        first = self.server_capability()
        refreshed = self.server_capability(
            now=first.issued_at + 1,
            lease_sequence=2,
        )
        renewals: queue.Queue = queue.Queue()

        class Revocation:
            value = first.expires_at

        gate = RuntimeLeaseGate(
            first,
            renewals,
            Revocation,
            trusted_public_keys=self.trusted_public_keys,
        )
        renewals.put(refreshed.to_wire())

        self.assertEqual(gate.value, refreshed.expires_at)
        self.assertEqual(gate.capability.lease_sequence, 2)

        forged = refreshed.to_wire()
        forged["lease_sequence"] = 3
        forged["expires_at"] += 60
        renewals.put(forged)

        self.assertEqual(gate.value, 0.0)
        self.assertTrue(gate.revoked)


if __name__ == "__main__":
    unittest.main()
