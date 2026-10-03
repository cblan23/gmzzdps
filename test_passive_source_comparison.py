import unittest
from tools.compare_passive_packet_sources import compare_tcp_ranges, merge_tcp_segments


class SourceComparisonTests(unittest.TestCase):
    def test_segmentation_changes_do_not_look_like_missing_payload(self):
        result = compare_tcp_ranges([(100, b"abcdef")], [(100, b"abc"), (103, b"def")])
        self.assertEqual(result["compared_bytes"], 6)
        self.assertEqual(result["different_bytes"], 0)
        self.assertEqual(result["candidate_missing_bytes"], 0)

    def test_duplicate_segments_are_compared_only_once(self):
        result = compare_tcp_ranges([(100, b"abcd"), (100, b"abcd")], [(100, b"abcd")])
        self.assertEqual(result["compared_bytes"], 4)
        self.assertEqual(result["conflicts"], 0)

    def test_missing_interior_is_reported(self):
        result = compare_tcp_ranges([(100, b"abcdef")], [(100, b"ab"), (104, b"ef")])
        self.assertEqual(result["candidate_missing_bytes"], 2)
        self.assertEqual(result["compared_bytes"], 4)

    def test_different_payload_is_reported(self):
        result = compare_tcp_ranges([(100, b"abcd")], [(100, b"abXd")])
        self.assertEqual(result["different_bytes"], 1)

    def test_sequence_wrap_is_normalized(self):
        ranges, errors = merge_tcp_segments([(0xfffffffe, b"ab"), (0, b"cd")])
        self.assertEqual(ranges, [(0xfffffffe, b"abcd")])
        self.assertEqual(errors, 0)

    def test_absent_candidate_does_not_pass(self):
        result = compare_tcp_ranges([(100, b"abcd")], [])
        self.assertEqual(result["candidate_missing_bytes"], 4)
        self.assertEqual(result["compared_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
