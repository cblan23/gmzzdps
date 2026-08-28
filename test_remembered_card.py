#!/usr/bin/env python3

from __future__ import annotations

import base64
import json
import sys
import unittest

from remembered_card import (
    CONFIG_KEY,
    protect_card_key,
    remember_card,
    remembered_card,
    unprotect_card_key,
)


@unittest.skipUnless(sys.platform == "win32", "Windows DPAPI is required")
class RememberedCardTests(unittest.TestCase):
    def test_dpapi_round_trip_does_not_store_plaintext(self):
        card_key = "GMZZABCDEFGHJKLMNPQRSTUVWXYZ23"
        encrypted = protect_card_key(card_key)

        self.assertTrue(encrypted)
        self.assertNotIn(card_key, encrypted)
        self.assertEqual(unprotect_card_key(encrypted), card_key)

    def test_config_round_trip_contains_only_encrypted_value(self):
        card_key = "doriapig"
        config = {"server_url": "https://daodaogame.vip"}

        self.assertTrue(remember_card(config, card_key))
        self.assertEqual(remembered_card(config), card_key)
        self.assertIn(CONFIG_KEY, config)
        self.assertNotIn(card_key, json.dumps(config))

    def test_invalid_or_tampered_values_are_ignored(self):
        self.assertEqual(unprotect_card_key("not-base64!"), "")
        encrypted = protect_card_key("doriapig")
        raw = bytearray(base64.b64decode(encrypted))
        raw[len(raw) // 2] ^= 0x01
        tampered = base64.b64encode(raw).decode("ascii")
        self.assertEqual(unprotect_card_key(tampered), "")

    def test_control_characters_are_not_saved(self):
        config = {}
        self.assertFalse(remember_card(config, "card\nvalue"))
        self.assertNotIn(CONFIG_KEY, config)


if __name__ == "__main__":
    unittest.main()
