"""Version pins for the read-only passive state layout."""

import unittest

from npcap_key_state import RC4_CRYPTOR_OWNER_VTABLE_RVA
from npcap_zstd_state import (
    CODEC_DCTX_POINTER_OFFSET,
    DCTX_CONTEXT_SIZE,
    ZSTD_CODEC_VTABLE_RVA,
)


class CurrentClientLayoutTests(unittest.TestCase):
    def test_resource_build_2307645_state_layout(self):
        self.assertEqual(RC4_CRYPTOR_OWNER_VTABLE_RVA, 0x0DB93488)
        self.assertEqual(ZSTD_CODEC_VTABLE_RVA, 0x0DBB00E8)
        self.assertEqual(CODEC_DCTX_POINTER_OFFSET, 0x28)
        self.assertEqual(DCTX_CONTEXT_SIZE, 0x176F0)


if __name__ == "__main__":
    unittest.main()
