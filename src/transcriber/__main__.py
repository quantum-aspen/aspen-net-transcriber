"""transcriber serve | transcriber run <url> [--no-agent]"""

import argparse
import logging
import sys
import threading
import time
from datetime import datetime

from . import hermes, inbox, pipeline
from .config import Config
from .outline import Outline, OutlineError
from .state import State

log = logging.getLogger("transcriber")


def in_window(hours: tuple[int, int] | None, now: datetime | None = None) -> bool:
    if hours is None:
        return True
    h = (now or datetime.now()).hour
    start, end = hours
    return start <= h < end if start < end else h >= start or h < end


def _heartbeat(cfg: Config) -> None:
    while True:
        cfg.heartbeat.write_text(str(time.time()))
        time.sleep(30)


class Poller:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = State(cfg.state_db)
        self.wiki = Outline(cfg.outline_url, cfg.outline_api_key)

    def show(self, url: str, status: str) -> None:
        """Mirror a job's status into the inbox doc. Best effort: the state db is the record."""
        try:
            doc = self.wiki.document(self.cfg.inbox_doc)
            new = inbox.set_status(doc["text"], url, status)
            if new != doc["text"]:
                self.wiki.update_text(doc["id"], new)
        except OutlineError as e:
            log.warning("couldn't update inbox status for %s: %s", url, e)

    def sync(self) -> None:
        """Move pasted links into the queue table, queue new and retried rows, and
        re-show finished jobs whose status was cleared."""
        doc = self.wiki.document(self.cfg.inbox_doc)
        text, adopted = inbox.adopt(doc["text"])
        if text != doc["text"]:
            self.wiki.update_text(doc["id"], text)
            log.info("moved %d pasted link(s) into the queue table", len(adopted))
        for row in inbox.parse(text):
            if not row.wants_run:
                continue
            job = self.state.get(row.url)
            retry = row.status.strip().lower() == "retry"
            if job and job.state == "done" and not retry:
                self.show(row.url, f"done — [summary]({job.wiki_url})")
                continue
            if job and job.state in ("queued", "working"):
                continue
            self.state.enqueue(row.url, row.series, retry=retry)
            log.info("queued %s", row.url)
            self.show(row.url, "queued")

    def work(self) -> bool:
        """Runs the next queued job, if any. Returns whether there was one."""
        job = self.state.next_queued()
        if not job:
            return False
        self.state.start(job.url)
        self.show(job.url, "working")
        log.info("working on %s (attempt %d)", job.url, job.attempts + 1)
        wd = pipeline.workdir(self.cfg, job.url)
        try:
            pipeline.prepare(self.cfg, job.url)
            result = pipeline.summarise(self.cfg, job.url, job.series, wd)
        except Exception as e:  # noqa: BLE001 — any failure is reported on the row and retried
            log.exception("job failed: %s", job.url)
            new = self.state.fail(job.url, str(e), self.cfg.max_attempts)
            first_line = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
            self.show(job.url, "queued (retrying)" if new == "queued" else f"failed: {first_line}")
            if new == "failed":
                pipeline.clean(wd, everything=False)
            return True
        self.state.finish(job.url, result["wiki_url"])
        self.show(job.url, f"done — [summary]({result['wiki_url']})")
        pipeline.clean(wd, everything=True)
        log.info("done %s -> %s", job.url, result["wiki_url"])
        return True

    def loop(self) -> None:
        if n := self.state.recover():
            log.info("re-queued %d interrupted job(s)", n)
        while True:
            try:
                self.sync()
                while in_window(self.cfg.run_hours) and self.work():
                    pass
            except OutlineError as e:
                log.warning("inbox unavailable: %s", e)
            time.sleep(self.cfg.poll_interval)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="transcriber", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="poll the Outline inbox and process recordings")
    r = sub.add_parser("run", help="process one recording now, outside the inbox")
    r.add_argument("url")
    r.add_argument("--series", default="")
    r.add_argument("--no-agent", action="store_true", help="stop after transcribing and pulling frames")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    cfg = Config.from_env()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    if missing := cfg.missing(agent=not getattr(a, "no_agent", False), inbox=a.cmd == "serve"):
        log.error("missing settings: %s", ", ".join(missing))
        return 2
    if not getattr(a, "no_agent", False):
        hermes.drop_bundled_skills()
        log.info("hermes config: %s", hermes.write_config(cfg))

    if a.cmd == "serve":
        threading.Thread(target=_heartbeat, args=(cfg,), daemon=True).start()
        Poller(cfg).loop()
        return 0

    wd = pipeline.prepare(cfg, a.url)
    log.info("prepared %s", wd)
    if a.no_agent:
        return 0
    result = pipeline.summarise(cfg, a.url, a.series, wd)
    log.info("result: %s", result)
    pipeline.clean(wd, everything=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
