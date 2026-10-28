import json
import time
from datetime import datetime

import pytest
import yaml
from pydantic import SecretStr

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


def test_state_backs_off_exponentially_then_fails(tmp_path):
    s = State(tmp_path / "s.db")
    s.enqueue(A, "x")
    delays = []
    for _ in range(3):
        s.start(A)
        before = time.time()
        job = s.fail(A, "boom", max_attempts=4, base_delay=900)
        assert job.state == "queued"
        assert s.next_queued(now=job.not_before - 1) is None
        assert s.next_queued(now=job.not_before).url == A
        delays.append(round(job.not_before - before, -1))
    assert delays == [900, 1800, 3600]
    s.start(A)
    assert s.fail(A, "boom", max_attempts=4, base_delay=900).state == "failed"
    s.enqueue(A, "x")  # plain enqueue doesn't resurrect it
    assert s.get(A).state == "failed"
    s.enqueue(A, "x", retry=True)
    assert (s.get(A).state, s.get(A).attempts, s.get(A).not_before) == ("queued", 0, 0)


def test_backoff_is_capped(tmp_path):
    s = State(tmp_path / "s.db")
    s.enqueue(A, "")
    for _ in range(10):
        s.start(A)
        job = s.fail(A, "boom", max_attempts=99, base_delay=900)
    assert job.not_before - time.time() <= 6 * 3600


def test_release_does_not_use_up_the_attempt(tmp_path):
    s = State(tmp_path / "s.db")
    s.enqueue(A, "")
    s.start(A)
    s.release(A)
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
    monkeypatch.setattr(pipeline, "prepare", lambda c, url, passcode="": None)
    monkeypatch.setattr(pipeline, "summarise", lambda c, url, series, wd: {"wiki_url": "https://wiki/doc/x"})
    assert p.work() is True
    assert p.state.get(A).state == "done"
    assert status(p, A) == "done — [summary](https://wiki/doc/x)"
    assert p.work() is False


def test_work_failure_retries_then_fails(cfg, monkeypatch):
    p = poller(cfg)
    p.sync()

    def boom(c, url, passcode=""):
        raise pipeline.JobError("fetch.sh exited 1:\nHTTP 404")

    monkeypatch.setattr(pipeline, "prepare", boom)
    p.work()
    assert status(p, A).startswith("queued (attempt 2 after ")
    assert p.work() is False  # backing off
    for _ in range(cfg.max_attempts - 1):
        p.state.db.execute("UPDATE jobs SET not_before = 0")
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


def test_keys_stay_out_of_reprs(tmp_path):
    c = Config.from_env({"OUTLINE_API_KEY": "ol-secret", "LUMEN_API_KEY": "lu-secret"})
    assert "secret" not in repr(c) and "secret" not in str(c)
    assert c.outline_api_key.get_secret_value() == "ol-secret"
    assert "secret" not in repr(svc.Outline("https://x", c.outline_api_key).__dict__)


def test_shutdown_mid_job_releases_it(cfg, monkeypatch):
    p = poller(cfg)
    p.sync()

    def stopped(c, url, passcode=""):
        raise svc.Shutdown("SIGTERM")

    monkeypatch.setattr(pipeline, "prepare", stopped)
    with pytest.raises(svc.Shutdown):
        p.work()
    assert (p.state.get(A).state, p.state.get(A).attempts) == ("queued", 0)
    assert status(p, A) == "queued (interrupted by a restart)"


def test_loop_survives_errors(cfg, monkeypatch):
    p = poller(cfg)
    calls = []

    def broken_sync():
        calls.append("sync")
        raise RuntimeError("bug")

    def stop(_):
        raise svc.Shutdown("test")

    monkeypatch.setattr(p, "sync", broken_sync)
    monkeypatch.setattr(p, "work", lambda: (_ for _ in ()).throw(ValueError("bug")))
    monkeypatch.setattr(svc.time, "sleep", stop)
    with pytest.raises(svc.Shutdown):
        p.loop()
    assert calls == ["sync"]


def test_outline_network_errors_become_outline_errors():
    from transcriber.outline import OutlineError

    wiki = svc.Outline("http://127.0.0.1:9", SecretStr("k"))  # nothing listens on port 9
    with pytest.raises(OutlineError, match="documents.info"):
        wiki.document("x")


def test_remote_asr_defaults_to_the_lumen_key(tmp_path, monkeypatch):
    c = Config.from_env({"LUMEN_API_KEY": "lu", "TRANSCRIBE_URL": "https://x/v1"})
    monkeypatch.delenv("TRANSCRIBE_URL", raising=False)
    assert "TRANSCRIBE_API_KEY" not in pipeline._asr_env(c)  # local: no key passed
    monkeypatch.setenv("TRANSCRIBE_URL", "https://x/v1")
    assert pipeline._asr_env(c)["TRANSCRIBE_API_KEY"] == "lu"
    monkeypatch.setenv("TRANSCRIBE_API_KEY", "own")
    assert "TRANSCRIBE_API_KEY" not in pipeline._asr_env(c)  # its own key wins via the inherited env


def test_write_team_keeps_names_and_ids_only(cfg, tmp_path, monkeypatch):
    users = [
        {"id": "2", "name": "Krister Shalm", "email": "k@x"},
        {"id": "1", "name": "Gina Lorenz", "email": "g@x"},
    ]
    monkeypatch.setattr(pipeline.Outline, "users", lambda self: users)
    pipeline.write_team(cfg, tmp_path)
    assert json.loads((tmp_path / "team.json").read_text()) == [
        {"name": "Gina Lorenz", "id": "1"}, {"name": "Krister Shalm", "id": "2"},
    ]  # fmt: skip


def test_write_team_is_best_effort(cfg, tmp_path, monkeypatch):
    def down(self):
        raise pipeline.OutlineError("users.list: URLError")

    monkeypatch.setattr(pipeline.Outline, "users", down)
    pipeline.write_team(cfg, tmp_path)
    assert not (tmp_path / "team.json").exists()


def test_dry_run_gets_read_only_outline_and_it_doesnt_leak(tmp_path):
    home = tmp_path / "hermes"
    base = {"OUTLINE_API_KEY": "k", "LUMEN_MODEL": "m", "TRANSCRIBER_DATA": str(tmp_path / "d")}
    dry = yaml.safe_load(hermes.write_config(Config.from_env(base | {"DRY_RUN": "1"}), home).read_text())
    assert dry["mcp_servers"]["outline"]["tools"]["include"] == list(hermes.OUTLINE_READ_TOOLS)
    assert not any(t.startswith(("create", "update", "delete", "move")) for t in hermes.OUTLINE_READ_TOOLS)
    real = yaml.safe_load(hermes.write_config(Config.from_env(base), home).read_text())
    assert "tools" not in real["mcp_servers"]["outline"]
    nokey = yaml.safe_load(
        hermes.write_config(Config.from_env(base | {"OUTLINE_API_KEY": ""}), home).read_text()
    )
    assert "outline" not in nokey["mcp_servers"]


def test_outline_sends_a_named_user_agent(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"data": {"id": "x", "text": ""}}'

    def fake_urlopen(req, timeout):
        seen.update(req.header_items())
        return Resp()

    from transcriber import outline

    monkeypatch.setattr(outline.urllib.request, "urlopen", fake_urlopen)
    outline.Outline("https://wiki", SecretStr("k")).document("x")
    assert seen["User-agent"].startswith("aspen-net-transcriber")


C = "https://illinois.zoom.us/rec/share/CCC"


def test_passcode_is_stored_not_shown_and_passed_to_prepare(cfg, monkeypatch):
    p = poller(cfg, f"- {C} passcode: s3cr3t Weekly sync\n")
    p.sync()
    assert "s3cr3t" not in p.wiki.text
    job = p.state.get(C)
    assert job.passcode == "s3cr3t" and job.series == "Weekly sync"
    assert "s3cr3t" not in repr(job)
    seen = {}
    monkeypatch.setattr(pipeline, "prepare", lambda c, url, passcode="": seen.setdefault("pc", passcode))
    monkeypatch.setattr(pipeline, "summarise", lambda c, url, series, wd: {"wiki_url": "https://wiki/doc/x"})
    p.work()
    assert seen["pc"] == "s3cr3t"


def test_missing_passcode_fails_at_once_and_a_repaste_requeues(cfg, monkeypatch):
    p = poller(cfg, f"- {C}\n")
    p.sync()

    def needs(c, url, passcode=""):
        raise pipeline.NeedsPasscode(f"needs a passcode: {pipeline.REPASTE}")

    monkeypatch.setattr(pipeline, "prepare", needs)
    p.work()
    assert p.state.get(C).state == "failed"  # no retries: they can't help
    assert status(p, C).startswith("failed: needs a passcode")

    p.wiki.text += f"- {C} passcode: abc\n"
    p.sync()
    assert p.state.get(C).state == "queued" and p.state.get(C).passcode == "abc"
    assert status(p, C) == "queued" and "abc" not in p.wiki.text


def test_prepare_passes_the_passcode_to_fetch_only(cfg, monkeypatch):
    calls = []

    def fake_run(cmd, wd, env=None, timeout=0):
        calls.append((cmd, env))
        if cmd[1].endswith("fetch.sh"):
            (wd / "meeting.wav").write_bytes(b"")
            (wd / "transcript.srt").write_text("")

    monkeypatch.setattr(pipeline, "_run", fake_run)
    pipeline.prepare(cfg, C, "s3cr3t")
    fetch_cmd, fetch_env = calls[0]
    assert fetch_env == {"ZOOM_PASSCODE": "s3cr3t"}
    assert all("s3cr3t" not in " ".join(cmd) for cmd, _ in calls)
    assert all(env is None or "ZOOM_PASSCODE" not in env for _, env in calls[1:])


def test_state_adds_passcode_column_to_an_old_database(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.execute(
        "CREATE TABLE jobs (url TEXT PRIMARY KEY, series TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, "
        "attempts INTEGER NOT NULL DEFAULT 0, wiki_url TEXT NOT NULL DEFAULT '', "
        "error TEXT NOT NULL DEFAULT '', not_before REAL NOT NULL DEFAULT 0, "
        "created REAL NOT NULL, updated REAL NOT NULL)"
    )
    old.execute("INSERT INTO jobs (url, state, created, updated) VALUES (?, 'done', 0, 0)", (A,))
    old.commit()
    old.close()
    s = State(db)
    assert s.get(A).passcode == ""
    assert s.set_passcode(A, "x") is True and s.set_passcode(A, "x") is False
