"""Versioned atomic encounter journal, separate from untouched legacy archives.

The JSON journal is authoritative. SQLite is a derived index, rebuilt on load;
crash between writes cannot turn its cached rows into the source of truth.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from contextlib import closing
import sqlite3
import threading
import uuid

from encounter_tracker import EncounterTracker


class EncounterRepository:
    def __init__(self, history_directory: str | Path):
        self.directory = Path(history_directory) / 'encounters-v3'
        self.journal = self.directory / 'journal.json'
        self.index = self.directory / 'index.sqlite3'
        self.lock = threading.RLock()

    def save(self, tracker: EncounterTracker) -> None:
        encoded = json.dumps(tracker.to_dict(), ensure_ascii=False, indent=2, allow_nan=False)
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            temp = self.directory / ('.journal-' + uuid.uuid4().hex + '.tmp')
            try:
                with temp.open('x', encoding='utf-8') as out:
                    out.write(encoded)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temp, self.journal)
            finally:
                temp.unlink(missing_ok=True)
            self._reindex(tracker)

    def load(self) -> EncounterTracker:
        with self.lock:
            if not self.journal.exists():
                return EncounterTracker()
            # Invalid data is an error, not an empty history to overwrite.
            data = json.loads(self.journal.read_text(encoding='utf-8'))
            tracker = EncounterTracker.from_dict(data)
            self._reindex(tracker)
            return tracker

    def _reindex(self, tracker: EncounterTracker) -> None:
        with closing(sqlite3.connect(self.index)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS encounters ('
                       'local_encounter_id TEXT PRIMARY KEY, server_battle_id TEXT, instance_id TEXT NOT NULL,'
                       'result TEXT NOT NULL, settlement_status TEXT NOT NULL, started_at_ns INTEGER NOT NULL,'
                       'ended_at_ns INTEGER, duration_seconds REAL, damage INTEGER, revision INTEGER NOT NULL)')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS encounter_server_identity '
                       'ON encounters(instance_id, server_battle_id) WHERE server_battle_id IS NOT NULL')
            db.execute('DELETE FROM encounters')
            for e in tracker.encounters.values():
                if e.history_deleted:
                    continue
                values = [r.get('damage') for r in e.participants]
                total = sum(values) if values and all(v is not None for v in values) else None
                db.execute('INSERT INTO encounters VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (e.local_encounter_id,e.server_battle_id,e.instance_id,e.result,e.settlement_status,
                     e.started_at_ns,e.ended_at_ns,e.encounter_duration_seconds,total,e.revision))

    def summaries(self) -> list[dict]:
        # Always synchronize derived state after a restart or partial write.
        self.load()
        if not self.index.exists():
            return []
        with closing(sqlite3.connect(self.index)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM encounters ORDER BY started_at_ns DESC')]
