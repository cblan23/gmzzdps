import threading
import unittest
from collections import Counter, deque
from npcap_receiver import BufferedReceiver
from passive_transport import CaptureFrame


class FakeReceiver:
    def __init__(self, frames):
        self.frames = deque(frames)
        self.handles = {}
        self.local_addresses = {'127.0.0.1'}
        self.endpoints = []
        self.counters = Counter()
        self.drained = threading.Event()
        self.refresh_done = threading.Event()
        self.closed_thread = None

    def next_frame(self):
        if self.frames:
            return self.frames.popleft()
        self.drained.set()
        self.refresh_done.wait(0.001)
        return None

    def statistics(self, _reader):
        return {'available': True, 'dropped': 0}

    def refresh(self, endpoints):
        self.endpoints = endpoints
        self.refresh_done.set()

    def close(self):
        self.closed_thread = threading.get_ident()


class BufferedReceiverTests(unittest.TestCase):
    def test_receive_continues_while_consumer_does_no_work(self):
        frames = [CaptureFrame(bytes([i]), i) for i in range(20)]
        raw = FakeReceiver(frames)
        reader = BufferedReceiver(raw, None)
        try:
            self.assertTrue(raw.drained.wait(2))
            self.assertEqual([reader.next_frame() for _ in frames], frames)
            self.assertEqual(reader.statistics()['queued_bytes'], 0)
        finally:
            reader.close()
        self.assertNotEqual(raw.closed_thread, threading.get_ident())

    def test_overflow_is_bounded_and_reported(self):
        raw = FakeReceiver([CaptureFrame(b'1234', i) for i in range(10)])
        reader = BufferedReceiver(raw, None, max_frames=2, max_bytes=8)
        try:
            self.assertTrue(raw.drained.wait(2))
            self.assertEqual(reader.statistics()['queued_bytes'], 8)
            self.assertEqual(reader.take_counters()['capture_queue_dropped'], 8)
            self.assertEqual(reader.take_counters()['capture_queue_dropped'], 0)
        finally:
            reader.close()

    def test_refresh_runs_in_receiver_thread(self):
        raw = FakeReceiver([])
        reader = BufferedReceiver(raw, None)
        try:
            reader.refresh(['new endpoint'])
            self.assertTrue(raw.refresh_done.wait(2))
            self.assertEqual(raw.endpoints, ['new endpoint'])
        finally:
            reader.close()


if __name__ == '__main__':
    unittest.main()
