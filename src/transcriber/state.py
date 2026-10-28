"""Job state in SQLite. The inbox Status column is only a display of this.

A job is keyed by its recording URL. States: queued -> working -> done | failed.
A failed attempt is re-queued with exponential backoff (not_before) until
max_attempts. A job stopped by a graceful shutdown is released without using
up the attempt. `working` rows found at startup were killed hard (OOM,
SIGKILL), so that attempt still counts: a job can't crash the service forever.

A recording's Zoom passcode, if it has one, is kept here only. The inbox
drops it from the doc, and it never goes in a log or a job's command line.
"""

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_DELAY = 6 * 3600
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    url        TEXT PRIMARY KEY,
    series     TEXT NOT NULL DEFAULT '',
    state      TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    wiki_url   TEXT NOT NULL DEFAULT '',
    error      TEXT NOT NULL DEFAULT '',
    not_before REAL NOT NULL DEFAULT 0,
    passcode   TEXT NOT NULL DEFAULT '',
    created    REAL NOT NULL,
    updated    REAL NOT NULL
)
"""
# Columns added since the first release, for databases created before them.
MIGRATIONS = {"passcode": "ALTER TABLE jobs ADD COLUMN passcode TEXT NOT NULL DEFAULT ''"}


@dataclass(frozen=True)
class Job:
    url: str
    series: str
    state: str
    attempts: int
    wiki_url: str
    error: str
    not_before: float
    passcode: str = field(default="", repr=False)


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute(SCHEMA)
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(jobs)")}
        for column, sql in MIGRATIONS.items():
            if column not in have:
                self.db.execute(sql)

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
                "UPDATE jobs SET state = 'queued', attempts = 0, error = '', not_before = 0, "
                "series = ?, updated = ? WHERE url = ? AND state IN ('done', 'failed')",
                (series, now, url),
            )

    def set_passcode(self, url: str, passcode: str) -> bool:
        """Stores url's passcode. Returns whether it changed."""
        return bool(
            self.db.execute(
                "UPDATE jobs SET passcode = ?, updated = ? WHERE url = ? AND passcode != ?",
                (passcode, time.time(), url, passcode),
            ).rowcount
        )

    def next_queued(self, now: float | None = None) -> Job | None:
        """The oldest queued job whose backoff has passed."""
        row = self.db.execute(
            "SELECT * FROM jobs WHERE state = 'queued' AND not_before <= ? ORDER BY created LIMIT 1",
            (time.time() if now is None else now,),
        ).fetchone()
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

    def fail(self, url: str, error: str, max_attempts: int, base_delay: float) -> Job:
        """Back to queued if attempts remain, after base_delay * 2^(attempts-1)
        (capped at 6 h), else failed. Returns the updated job."""
        job = self.get(url)
        now = time.time()
        if job and job.attempts < max_attempts:
            new, not_before = "queued", now + min(base_delay * 2 ** (job.attempts - 1), MAX_DELAY)
        else:
            new, not_before = "failed", 0
        self.db.execute(
            "UPDATE jobs SET state = ?, error = ?, not_before = ?, updated = ? WHERE url = ?",
            (new, error[:2000], not_before, now, url),
        )
        return self.get(url)

    def release(self, url: str) -> None:
        """Put a job interrupted by a graceful shutdown back, without using up the attempt."""
        self.db.execute(
            "UPDATE jobs SET state = 'queued', attempts = MAX(attempts - 1, 0), updated = ? "
            "WHERE url = ? AND state = 'working'",
            (time.time(), url),
        )

    def recover(self) -> int:
        """Re-queue jobs left `working` by a restart."""
        return self.db.execute(
            "UPDATE jobs SET state = 'queued', updated = ? WHERE state = 'working'", (time.time(),)
        ).rowcount
