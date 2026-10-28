from datetime import datetime

import pytest
import yaml

from transcriber import __main__ as svc
from transcriber import hermes, inbox, pipeline
from transcriber.config import Config
from transcriber.state import State

A = "https://illinois.zoom.us/rec/share/AAA"
B = "https://illinois.zoom.us/rec/share/BBB"
DOC = f"""| Recording | Series | Status |
|---|---|---|
| {A} | Software stack meeting | |
| {B} | | done |
"""


@pytest.fixture
def cfg(tmp_path):
    return Config.from_env({
        "OUTLINE_API_KEY": "k", "INBOX_DOC": "inbox", "LUMEN_MODEL": "m",
        "TRANSCRIBER_DATA": str(tmp_path / "data"), "SKILLS_DIR": str(tmp_path / "skills"),
    })  # fmt: skip


class FakeOutline:
    def __init__(self, text):
        self.text = text

    def document(self, _id):
        return {"id": "doc-uuid", "text": self.text}

    def update_text(self, _id, text):
        self.text = text


def poller(cfg, text=DOC):
    p = svc.Poller(cfg)
    p.wiki = FakeOutline(text)
    return p


def status(p, url):
    return next(r.status for r in inbox.parse(p.wiki.text) if r.url == url)


def test_state_attempts_then_failed(tmp_path):
    s = State(tmp_path / "s.db")
    s.enqueue(A, "x")
    s.start(A)
    assert s.fail(A, "boom", max_attempts=2) == "queued"
    s.start(A)
    assert s.fail(A, "boom", max_attempts=2) == "failed"
    s.enqueue(A, "x")  # plain enqueue doesn't resurrect it
    assert s.get(A).state == "failed"
    s.enqueue(A, "x", retry=True)
    assert (s.get(A).state, s.get(A).attempts) == ("queued", 0)


def test_recover_requeues_working(tmp_path):
    s = State(tmp_path / "s.db")
    s.enqueue(A, "")
    s.start(A)
    assert State(tmp_path / "s.db").recover() == 1
    assert s.get(A).state == "queued"


@pytest.mark.parametrize(
    "hours,hour,ok",
    [(None, 12, True), ((22, 6), 23, True), ((22, 6), 3, True), ((22, 6), 12, False), ((1, 5), 5, False)],
)
def test_in_window(hours, hour, ok):
    assert svc.in_window(hours, datetime(2026, 1, 1, hour)) is ok


def test_write_config_merges_and_keeps_key_out(cfg, tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model:\n  provider: openrouter\n  extra: kept\nagent:\n  x: 1\n")
    data = yaml.safe_load(hermes.write_config(cfg, home).read_text())
    assert data["model"] == {
        "provider": "custom", "extra": "kept", "base_url": "https://lumen.ncsa.illinois.edu/v1",
        "default": "m", "key_env": "LUMEN_API_KEY",
    }  # fmt: skip
    assert data["agent"] == {"x": 1}
    assert data["mcp_servers"]["outline"]["headers"]["Authorization"] == "Bearer ${OUTLINE_API_KEY}"
    assert "k" not in (home / "config.yaml").read_text().split()


def test_sync_queues_only_rows_that_want_a_run(cfg):
    p = poller(cfg)
    p.sync()
    assert p.state.get(A).state == "queued"
    assert p.state.get(B) is None
    assert status(p, A) == "queued"


def test_work_success_marks_done_and_links(cfg, monkeypatch):
    p = poller(cfg)
    p.sync()
    monkeypatch.setattr(pipeline, "prepare", lambda c, url: None)
    monkeypatch.setattr(pipeline, "summarise", lambda c, url, series, wd: {"wiki_url": "https://wiki/doc/x"})
    assert p.work() is True
    assert p.state.get(A).state == "done"
    assert status(p, A) == "done — [summary](https://wiki/doc/x)"
    assert p.work() is False


def test_work_failure_retries_then_fails(cfg, monkeypatch):
    p = poller(cfg)
    p.sync()

    def boom(c, url):
        raise pipeline.JobError("fetch.sh exited 1:\nHTTP 404")

    monkeypatch.setattr(pipeline, "prepare", boom)
    p.work()
    assert status(p, A) == "queued (retrying)"
    p.work()
    assert p.state.get(A).state == "failed"
    assert status(p, A) == "failed: fetch.sh exited 1:"


def test_done_job_with_cleared_status_is_reshown_not_rerun(cfg):
    p = poller(cfg)
    p.state.enqueue(A, "")
    p.state.start(A)
    p.state.finish(A, "https://wiki/doc/x")
    p.sync()
    assert p.state.get(A).state == "done"
    assert status(p, A) == "done — [summary](https://wiki/doc/x)"


def test_retry_requeues_done_job(cfg):
    p = poller(
        cfg, DOC.replace(f"| {A} | Software stack meeting | |", f"| {A} | Software stack meeting | retry |")
    )
    p.state.enqueue(A, "")
    p.state.start(A)
    p.state.finish(A, "https://wiki/doc/x")
    p.sync()
    assert p.state.get(A).state == "queued"


def test_prompt_fills_every_placeholder(cfg, tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    text = pipeline.prompt(cfg, A, "", wd).read_text()
    assert "$" not in text.replace("$workdir", "")
    assert A in text and str(wd) in text


def test_sync_adopts_pasted_links(cfg):
    C = "https://illinois.zoom.us/rec/share/CCC"
    p = poller(cfg, f"Paste links:\n\n- {C} Weekly sync\n")
    p.sync()
    assert status(p, C) == "queued"
    assert p.state.get(C).series == "Weekly sync"
    assert f"- {C}" not in p.wiki.text
