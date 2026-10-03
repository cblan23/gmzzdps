import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from encounter_repository import EncounterRepository
from encounter_tracker import EncounterTracker
from test_encounter_settlement import wipe, begin, normalized


class RepositoryTests(unittest.TestCase):
    def test_pending_restart_then_settle_preserves_legacy_files(self):
        with tempfile.TemporaryDirectory() as directory:
            old=Path(directory)/'old-battle.json';old.write_text('{"legacy":true}')
            repo=EncounterRepository(directory);t=EncounterTracker();e=wipe(t);begin(t,200)
            repo.save(t)
            restored=repo.load();restored.accept(normalized());repo.save(restored)
            rows={r['local_encounter_id']:r for r in repo.summaries()}
            self.assertEqual(rows[e.local_encounter_id]['damage'],200)
            self.assertIsNone(rows[restored.current_id]['damage'])
            self.assertEqual(rows[e.local_encounter_id]['duration_seconds'],10)
            self.assertEqual(old.read_text(),'{"legacy":true}')

    def test_index_is_rebuilt_from_journal_not_stale_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            repo=EncounterRepository(directory);t=EncounterTracker();wipe(t);repo.save(t)
            with closing(sqlite3.connect(repo.index)) as db, db:db.execute('UPDATE encounters SET damage=999')
            self.assertIsNone(repo.summaries()[0]['damage'])

    def test_corrupt_journal_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            repo=EncounterRepository(directory);repo.directory.mkdir();repo.journal.write_text('{invalid')
            with self.assertRaises(ValueError):repo.load()
            self.assertEqual(repo.journal.read_text(),'{invalid')

if __name__=='__main__':unittest.main()
