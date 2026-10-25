import queue
import struct
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from network_capture import (
    MESSAGE_ARGUMENT_SYNC_ENABLED_OFFSET,
    MESSAGE_ARGUMENT_SYNC_STATE_OFFSET,
    MESSAGE_COMMIT_OFFSET,
    MESSAGE_MAGIC,
    MESSAGE_RECORD_SIZE,
    MESSAGE_RECORDS_OFFSET,
    NetworkMessageHook,
)
from test_combat_model import DpsWindow, MODULE
from test_inline_capture import RUNTIME_PROFILE


class CaptureUiIsolationTests(unittest.TestCase):
    def test_identity_observer_never_enables_argument_waits_or_reads_arguments(self):
        hook = NetworkMessageHook(
            profile=RUNTIME_PROFILE, pid=1234, synchronize_arguments=False
        )
        hook.process = 99
        hook.ring = 0x500000
        hook.installed = True
        method = b"RetCommonCombatStatisticsByTeam"
        raw = bytearray(MESSAGE_RECORD_SIZE)
        struct.pack_into("<8Q", raw, 0, 0, 134321845085764054,
                         0x880088, 2, 3, 0x600000, 5, len(method))
        raw[0x40:0x40 + len(method)] = method
        struct.pack_into("<Q", raw, MESSAGE_ARGUMENT_SYNC_STATE_OFFSET, 3)
        struct.pack_into("<Q", raw, MESSAGE_COMMIT_OFFSET, 1)

        def read(_process, address, size):
            if address == hook.ring and size == 0x30:
                return struct.pack("<8sQQQQQ", MESSAGE_MAGIC, 1, 0, 0, 0, 0)
            if address == hook.ring + MESSAGE_RECORDS_OFFSET:
                return bytes(raw)
            self.fail("identity observation must not read the argument graph")

        with (
            patch("network_capture.process_alive", return_value=True),
            patch("network_capture.read_region", side_effect=read),
            patch("network_capture.write_memory") as write,
            patch("network_capture.RemoteMsgpackReader.decode") as decode,
        ):
            records = hook.poll()

        self.assertEqual(records[0]["script_entity"], 0x880088)
        self.assertNotIn("decoded_arguments", records[0])
        write.assert_not_called()
        decode.assert_not_called()

    def test_identity_observer_disables_waits_left_by_a_previous_reader(self):
        hook = NetworkMessageHook(
            profile=RUNTIME_PROFILE, pid=1234, synchronize_arguments=False
        )
        hook.process = 99
        hook.ring = 0x500000
        hook.installed = True
        with (
            patch("network_capture.process_alive", return_value=True),
            patch("network_capture.read_region", return_value=struct.pack(
                "<8sQQQQQ", MESSAGE_MAGIC, 0, 0, 0, 0, 1
            )),
            patch("network_capture.write_memory") as write,
        ):
            self.assertEqual(hook.poll(), [])
        write.assert_called_once_with(
            99, hook.ring + MESSAGE_ARGUMENT_SYNC_ENABLED_OFFSET,
            struct.pack("<Q", 0),
        )

    def test_identity_observer_rejects_unsafe_argument_decoding(self):
        hook = NetworkMessageHook(
            profile=RUNTIME_PROFILE, synchronize_arguments=False
        )
        with self.assertRaises(ValueError):
            hook.poll(decode_arguments=True)
        with self.assertRaises(ValueError):
            hook.poll(include_snapshots=True)

    def test_layout_failure_does_not_stop_control_messages_or_capture_renewal(self):
        callbacks = []
        events = []
        window = object.__new__(DpsWindow)
        window.closing = False
        window.root = SimpleNamespace(after=lambda delay, callback: callbacks.append(
            (delay, callback)
        ))
        window.messages = queue.Queue()
        window.control_messages = queue.Queue()
        window.worker = Mock()
        window._release_stalled_startup_gate = Mock(side_effect=[
            tk.TclError("layout changed during monitor transition"), None
        ])
        window._check_license_network_recovery = Mock()
        capability = object.__new__(MODULE["RuntimeCapability"])
        with patch.dict(DpsWindow._drain_messages.__globals__, {
            "write_capture_lifecycle_event": lambda message, **fields: events.append(
                (message, fields)
            )
        }):
            window._drain_messages()
            self.assertEqual(len(callbacks), 1)
            window.control_messages.put(("runtime_capability", capability))
            callbacks[0][1]()

        window.worker.refresh_runtime_capability.assert_called_once_with(capability)
        self.assertEqual(len(callbacks), 2)
        self.assertEqual(events[0][0], "message_drain_failed")
        self.assertEqual(events[0][1]["error_type"], "TclError")


if __name__ == "__main__":
    unittest.main()
