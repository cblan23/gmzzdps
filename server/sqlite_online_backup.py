#!/usr/bin/env python3
"""Create a consistent SQLite backup while the service remains online."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: sqlite_online_backup.py SOURCE DESTINATION")
    source_path = Path(sys.argv[1])
    destination_path = Path(sys.argv[2])
    with sqlite3.connect(source_path) as source:
        with sqlite3.connect(destination_path) as destination:
            source.backup(destination)


if __name__ == "__main__":
    main()
