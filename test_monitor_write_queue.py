import io
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
from server import dps_monitor_server as monitor

class WriteQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.patch=mock.patch.object(monitor,'DATABASE_PATH',Path(self.directory.name)/'test.db')
        self.patch.start();monitor.initialize_database()
        self.handler=object.__new__(monitor.MonitorHandler)
        self.handler.path='/api/v2/dps/combat/clock'
        self.handler.wfile=io.BytesIO()
        self.handler._headers=mock.Mock()
        self.handler._database_busy_response=mock.Mock()
    def tearDown(self):
        self.patch.stop();self.directory.cleanup()
    @staticmethod
    def insert(connection,key):
        connection.execute('INSERT INTO clients(client_id,display_name,first_seen,last_seen) VALUES(?,?,1,1)',(key,'test'))
    def test_slow_http_write_does_not_hold_database_writer(self):
        blocked=threading.Event();release=threading.Event()
        class SlowWriter:
            def write(self,_):
                blocked.set()
                if not release.wait(3):raise RuntimeError('test response wait expired')
        self.handler.wfile=SlowWriter()
        def request():
            with monitor.write_database() as connection:
                self.insert(connection,'first')
                self.handler._json(200,{'ok':True})
        with ThreadPoolExecutor(max_workers=1) as executor:
            future=executor.submit(self.handler._run_request,request)
            try:
                self.assertTrue(blocked.wait(2))
                with mock.patch.object(monitor,'SQLITE_WRITE_QUEUE_TIMEOUT_SECONDS',.1):
                    with monitor.write_database() as connection:
                        self.insert(connection,'second')
                with monitor.database() as connection:
                    self.assertEqual(connection.execute('SELECT COUNT(*) FROM clients').fetchone()[0],2)
            finally:release.set()
            future.result(timeout=2)
    def test_failed_commit_never_sends_success(self):
        class FailingCommit(sqlite3.Connection):
            def commit(self):raise sqlite3.OperationalError('database is locked')
        def connect():
            return sqlite3.connect(monitor.DATABASE_PATH,factory=FailingCommit)
        def request():
            with monitor.write_database() as connection:
                self.insert(connection,'rolled-back')
                self.handler._json(200,{'ok':True})
        with mock.patch.object(monitor,'_open_database',side_effect=connect):
            self.handler._run_request(request)
        self.handler._headers.assert_not_called()
        self.handler._database_busy_response.assert_called_once()
        self.assertEqual(self.handler.wfile.getvalue(),b'')
        with monitor.database() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM clients').fetchone()[0],0)
    def test_write_exception_clears_deferred_response(self):
        def request():
            with monitor.write_database():
                self.handler._json(200,{'ok':True})
                raise ValueError('fail before commit')
        with self.assertRaises(ValueError):self.handler._run_request(request)
        self.handler._headers.assert_not_called()
        self.handler._run_request(lambda:self.handler._json(200,{'next':True}))
        self.assertEqual(self.handler.wfile.getvalue(),b'{"next":true}')
    def test_cleanup_is_indexed_and_bounded(self):
        limit=monitor.COMBAT_CLOCK_CLEANUP_BATCH_SIZE
        with monitor.write_database() as connection:
            for table in ('combat_clocks','combat_clocks_v2'):
                connection.executemany(f'INSERT INTO {table}(clock_id,party_key,target_key,started_at,created_at,last_seen) VALUES(?,?,?,?,?,?)',
                    [(str(i),'party','target',1,1,1) for i in range(limit+3)])
                plan=' '.join(row[3] for row in connection.execute('EXPLAIN QUERY PLAN SELECT clock_id FROM '+table+' WHERE last_seen<? ORDER BY last_seen LIMIT ?',(2,limit)))
                self.assertIn('cleanup',plan)
            with mock.patch.object(monitor,'_COMBAT_CLOCK_CLEANUP_STATE',None):
                self.assertTrue(monitor.cleanup_expired_combat_clocks(connection,2_000_000_000))
            for table in ('combat_clocks','combat_clocks_v2'):
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],3)
    def test_session_card_lookup_uses_card_index(self):
        with monitor.database() as connection:
            plan=' '.join(row[3] for row in connection.execute('EXPLAIN QUERY PLAN SELECT session_id FROM sessions WHERE card_hash=? AND ended_at IS NULL AND last_seen>=?',('card',0)))
        self.assertIn('idx_sessions_card_active',plan)

if __name__=='__main__':unittest.main()
