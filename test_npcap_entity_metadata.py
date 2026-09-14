import struct
import unittest
from collections import Counter
from npcap_entity_metadata import decode_component, PassiveEntityMetadataReader


class EntityMetadataTests(unittest.TestCase):
    def raw(self):
        raw=bytearray(0x19c)
        struct.pack_into('<Q',raw,0,0x1100)
        struct.pack_into('<Q',raw,0x10,0x3000)
        struct.pack_into('<Q',raw,0x58,12345)
        struct.pack_into('<I',raw,0x198,7114223)
        raw[0x137]=3
        return raw

    def test_exact_known_template(self):
        self.assertEqual(decode_component(self.raw(),12345,0x1000,0x1000,{'7114223':{}})[:3],(12345,7114223,3))

    def test_unrelated_object_or_unknown_template_is_rejected(self):
        self.assertIsNone(decode_component(self.raw(),54321,0x1000,0x1000,{'7114223':{}}))
        self.assertIsNone(decode_component(self.raw(),12345,0x1000,0x1000,{}))
        self.assertIsNone(decode_component(self.raw(),12345,0x5000,0x1000,{'7114223':{}}))

    def test_truncated_read_is_rejected(self):
        self.assertIsNone(decode_component(self.raw()[:-1],12345,0x1000,0x1000,{'7114223':{}}))

    def test_stage_template_change_is_emitted_for_same_entity(self):
        reader = object.__new__(PassiveEntityMetadataReader)
        reader.base, reader.size = 0x1000, 0x1000
        reader.templates = {'7114223': {}, '7114225': {}}
        first = decode_component(self.raw(), 12345, reader.base, reader.size, reader.templates)
        reader.resolved = {12345}
        reader.components = {12345: 0x4000}
        reader.signatures = {12345: first}
        reader.checked_at = {}
        reader.ready = []
        reader.pending = {}
        reader.retry_at = 0
        reader.counters = Counter()
        changed = self.raw()
        struct.pack_into('<I', changed, 0x198, 7114225)
        reader._read = lambda *_: changed
        reader.request(12345, 123456)
        updates = reader.poll()
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]['entity_id'], 12345)
        self.assertEqual(updates[0]['template_id'], 7114225)
        self.assertEqual(reader.poll(), [])


if __name__=='__main__':unittest.main()
