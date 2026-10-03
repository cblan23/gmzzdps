"""Bootstrap policy tests use mock readers only, never access a game."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from npcap_bootstrap import bootstrap_copies, PassiveStateError, FrozenSessionState
from npcap_capture_process import _install_state_snapshot


class BootstrapTests(unittest.TestCase):
    def reader(self):
        return SimpleNamespace(rc4=object(),zstd=object(),cryptor_address=123,close=Mock())

    def test_all_handles_closed_before_return_and_install_has_no_reader(self):
        r=self.reader();coherent=Mock(return_value=('anchor','compression'))
        snapshots,diagnostic=bootstrap_copies(1,lambda pid:([r],{}),coherent)
        r.close.assert_called_once()
        self.assertFalse(hasattr(snapshots[0],'process'))
        decoder,stream=Mock(),Mock()
        _install_state_snapshot(snapshots[0],stream,decoder,10)
        decoder.install_zstd_snapshot.assert_called_once_with('compression')
        stream.install_anchor.assert_called_once_with('anchor',10)
        coherent.assert_called_once()

    def test_failed_candidate_closed_and_not_retried(self):
        a,b=self.reader(),self.reader()
        snapshots,_=bootstrap_copies(1,lambda pid:([a,b],{}),Mock(side_effect=[RuntimeError('bad'),('a','z')]))
        self.assertEqual(len(snapshots),1)
        a.close.assert_called_once();b.close.assert_called_once()

    def test_no_snapshot_fails_closed(self):
        r=self.reader()
        with self.assertRaises(PassiveStateError):
            bootstrap_copies(1,lambda pid:([r],{}),Mock(side_effect=RuntimeError('bad')))
        r.close.assert_called_once()

    def test_live_reader_cannot_be_installed_in_runtime(self):
        with self.assertRaises(PassiveStateError):
            _install_state_snapshot(self.reader(),Mock(),Mock())

    def test_cleanup_failure_does_not_return_usable_snapshot(self):
        r=self.reader();r.close.side_effect=RuntimeError('unclosed')
        with self.assertRaises(PassiveStateError):
            bootstrap_copies(1,lambda pid:([r],{}),lambda a,b:('a','z'))

if __name__=='__main__':unittest.main()
