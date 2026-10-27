"""Job state in SQLite. The inbox Status column is only a display of this.

A job is keyed by its recording URL. States: queued -> working -> done | failed.
`working` rows found at startup were interrupted (container restart) and go
back to queued; they still count against max_attempts.
"""

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    url        TEXT PRIMARY KEY,
    series     TEXT NOT NULL DEFAULT '',
    state      TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    wiki_url   TEXT NOT NULL DEFAULT '',
    error      TEXT NOT NULL DEFAULT '',
    created    REAL NOT NULL,
    updated    REAL NOT NULL
)
"""


@dataclass(frozen=True)
class Job:
    url: str
    series: str
    state: str
    attempts: int
    wiki_url: str
    error: str


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute(SCHEMA)

    def _job(self, row) -> Job | None:
        return Job(**{k: row[k] for k in Job.__dataclass_fields__}) if row else None

    def get(self, url: str) -> Job | None:
        return self._job(self.db.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone())

    def enqueue(self, url: str, series: str, *, retry: bool = False) -> None:
        """New URLs are queued. retry re-queues a finished one with a fresh attempt count."""
        now = time.time()
        self.db.execute(
            "INSERT INTO jobs (url, series, state, created, updated) VALUES (?, ?, 'queued', ?, ?) "
            "ON CONFLICT(url) DO NOTHING",
            (url, series, now, now),
        )
        if retry:
            self.db.execute(
                "UPDATE jobs SET state = 'queued', attempts = 0, error = '', series = ?, updated = ? "
                "WHERE url = ? AND state IN ('done', 'failed')",
                (series, now, url),
            )

    def next_queued(self) -> Job | None:
        row = self.db.execute("SELECT * FROM jobs WHERE state = 'queued' ORDER BY created LIMIT 1").fetchone()
        return self._job(row)

    def start(self, url: str) -> None:
        self.db.execute(
            "UPDATE jobs SET state = 'working', attempts = attempts + 1, updated = ? WHERE url = ?",
            (time.time(), url),
        )

    def finish(self, url: str, wiki_url: str) -> None:
        self.db.execute(
            "UPDATE jobs SET state = 'done', wiki_url = ?, error = '', updated = ? WHERE url = ?",
            (wiki_url, time.time(), url),
        )

    def fail(self, url: str, error: str, max_attempts: int) -> str:
        """Back to queued if attempts remain, else failed. Returns the new state."""
        job = self.get(url)
        new = "queued" if job and job.attempts < max_attempts else "failed"
        self.db.execute(
            "UPDATE jobs SET state = ?, error = ?, updated = ? WHERE url = ?",
            (new, error[:2000], time.time(), url),
        )
        return new

    def recover(self) -> int:
        """Re-queue jobs left `working` by a restart."""
        return self.db.execute(
            "UPDATE jobs SET state = 'queued', updated = ? WHERE state = 'working'", (time.time(),)
        ).rowcount
