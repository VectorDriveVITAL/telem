"""Durable operational state for the single-process fleet controller.

InfluxDB owns samples; SQLite owns registrations, rules, current state and
operator activity. A transaction replaces one coherent snapshot. The controller
serializes mutations with an RLock, including background simulator ticks.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .config import settings


def _connect():
    path = Path(settings.state_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
    )
    return conn


def load():
    with closing(_connect()) as conn:
        row = conn.execute("SELECT payload FROM state WHERE id = 1").fetchone()
    return json.loads(row[0]) if row else None


def save(snapshot):
    payload = json.dumps(snapshot, allow_nan=False)
    with closing(_connect()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO state VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (payload,),
            )
