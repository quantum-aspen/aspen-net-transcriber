"""One meeting, start to finish.

The skill's scripts do the deterministic work (download, whisper, chat,
frames) with no model involved. Only the summary step runs the agent, so
meeting content goes to the model provider once, as text and a few frames.
Each step skips itself if its output exists, so a re-queued job resumes.
"""

import hashlib
import json
import logging
import os
import shutil
import sys
from importlib.resources import files
from pathlib import Path
from string import Template

from . import hermes, proc
from .config import Config
from .outline import Outline, OutlineError

log = logging.getLogger(__name__)
MEDIA = ("meeting.mp4", "share.mp4", "meeting.wav")


class JobError(RuntimeError):
    pass


REPASTE = "paste the link again with `passcode: …` on the same line"


class NeedsPasscode(JobError):
    """The recording's passcode is missing or wrong. Retrying won't help until
    someone pastes the link again with the right one."""


def workdir(cfg: Config, url: str) -> Path:
    return cfg.work_dir / hashlib.sha256(url.encode()).hexdigest()[:12]


def _run(cmd: list[str], wd: Path, env: dict | None = None, timeout: int = 3600) -> None:
    log.info("[%s] $ %s", wd.name, " ".join(cmd))
    rc = proc.run(
        cmd, cwd=wd, log_file=wd / "pipeline.log", tag=wd.name, env=os.environ | (env or {}), timeout=timeout
    )
    if rc:
        name = Path(cmd[1]).name if cmd[0] in ("sh", sys.executable) else cmd[0]
        tail = (wd / "pipeline.log").read_text(errors="replace")[-1500:]
        raise JobError(f"{name} exited {rc}:\n{tail}")


def _asr_env(cfg: Config) -> dict:
    """transcribe.py runs whisper locally unless TRANSCRIBE_URL names a server
    (see the script). A remote server defaults to the Lumen key."""
    env = {"HF_HOME": str(cfg.data_dir / "hf")}
    if os.environ.get("TRANSCRIBE_URL") and not os.environ.get("TRANSCRIBE_API_KEY"):
        env["TRANSCRIBE_API_KEY"] = cfg.lumen_api_key.get_secret_value()
    return env


def prepare(cfg: Config, url: str, passcode: str = "") -> Path:
    """Download, transcribe, and pull chat and frames. Returns the work dir.

    The passcode goes to fetch.sh as ZOOM_PASSCODE, and to nothing else."""
    wd = workdir(cfg, url)
    wd.mkdir(parents=True, exist_ok=True)
    scripts = cfg.skill_dir / "scripts"

    if not (wd / "meeting.wav").exists() and not (wd / "transcript.srt").exists():
        try:
            _run(
                ["sh", str(scripts / "fetch.sh"), url, str(wd)],
                wd,
                env={"ZOOM_PASSCODE": passcode} if passcode else None,
                timeout=2400,
            )
        except JobError as e:
            if "needs a passcode" in str(e):
                raise NeedsPasscode(f"needs a passcode: {REPASTE}") from e
            if "rejected the passcode" in str(e):
                raise NeedsPasscode(f"wrong passcode: {REPASTE}") from e
            raise

    if not (wd / "transcript.srt").exists():
        _run(
            [sys.executable, str(scripts / "transcribe.py"), "meeting.wav", "large-v3-turbo", "transcript"],
            wd,
            env=_asr_env(cfg),
            timeout=3 * 3600,
        )

    video = wd / ("share.mp4" if (wd / "share.mp4").exists() else "meeting.mp4")
    frames = [sys.executable, str(scripts / "frames.py")]
    if video.exists():
        if (wd / "chat.json").exists() and not (wd / "frames" / "commands.json").exists():
            _run([*frames, "commands", video.name, "chat.json", "frames"], wd)
        if (wd / "play_info.json").exists() and not (wd / "frames" / "scenes.json").exists():
            _run([*frames, "scenes", video.name, "play_info.json", "frames"], wd)

    (wd / "meeting.wav").unlink(
        missing_ok=True
    )  # whisper is done with it; the video stays for `frames.py at`
    return wd


def prompt(cfg: Config, url: str, series: str, wd: Path) -> Path:
    text = Template(files("transcriber").joinpath("prompts/summary.md").read_text()).substitute(
        url=url,
        series=series or "(not given — match it from the meeting content and the Meetings collection)",
        workdir=wd,
        skill=cfg.skill_dir,
        out=cfg.meetings_dir,
        publish=(
            "DRY RUN: you can read the wiki (the Outline tools are read-only), but don't create, "
            "update or upload anything. Load the template and series page as for a real run, write "
            "the finished summary body to `summary.md` in the work directory, and put that file's "
            "path in `wiki_url`. If step 1 finds the meeting already on the wiki, carry on anyway: "
            "the dry run is being compared against it. Don't read that existing summary."
            if cfg.dry_run
            else "Publish the summary to the wiki as the skill's ASPEN-NET output reference describes."
        ),
    )
    p = wd / "prompt.md"
    p.write_text(text)
    return p


def write_team(cfg: Config, wd: Path) -> None:
    """The wiki's users are the team roster the skill resolves names against.
    Names and ids only. Best effort: without it the agent falls back to the MCP."""
    if not cfg.outline_api_key.get_secret_value():
        return
    try:
        users = Outline(cfg.outline_url, cfg.outline_api_key).users()
    except OutlineError as e:
        log.warning("[%s] couldn't fetch the team roster: %s", wd.name, e)
        return
    team = sorted(({"name": u["name"], "id": u["id"]} for u in users), key=lambda u: u["name"])
    (wd / "team.json").write_text(json.dumps(team, indent=1) + "\n")


def summarise(cfg: Config, url: str, series: str, wd: Path) -> dict:
    """Runs the agent and returns its result.json."""
    (wd / "result.json").unlink(missing_ok=True)
    write_team(cfg, wd)
    rc = hermes.run(cfg, prompt(cfg, url, series, wd), wd, wd / "agent.log")
    try:
        result = json.loads((wd / "result.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError) as e:
        tail = (wd / "agent.log").read_text(errors="replace")[-1500:] if (wd / "agent.log").exists() else ""
        raise JobError(f"agent exited {rc} without a usable result.json ({e}):\n{tail}") from e
    if result.get("error"):
        raise JobError(f"agent: {result['error']}")
    if not result.get("wiki_url"):
        raise JobError(f"agent result has no wiki_url: {result}")
    return result


def clean(wd: Path, *, everything: bool) -> None:
    if everything:
        shutil.rmtree(wd, ignore_errors=True)
        return
    for name in MEDIA:
        (wd / name).unlink(missing_ok=True)
