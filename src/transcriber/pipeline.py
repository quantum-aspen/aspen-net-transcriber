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
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from string import Template

from . import hermes
from .config import Config

log = logging.getLogger(__name__)
MEDIA = ("meeting.mp4", "share.mp4", "meeting.wav")


class JobError(RuntimeError):
    pass


def workdir(cfg: Config, url: str) -> Path:
    return cfg.work_dir / hashlib.sha256(url.encode()).hexdigest()[:12]


def _run(cmd: list[str], wd: Path, env: dict | None = None, timeout: int = 3600) -> None:
    log.info("$ %s", " ".join(cmd))
    with (wd / "pipeline.log").open("ab") as out:
        rc = subprocess.run(
            cmd, cwd=wd, env=os.environ | (env or {}), stdout=out, stderr=subprocess.STDOUT, timeout=timeout
        ).returncode
    if rc:
        name = Path(cmd[1]).name if cmd[0] in ("sh", sys.executable) else cmd[0]
        tail = (wd / "pipeline.log").read_text(errors="replace")[-1500:]
        raise JobError(f"{name} exited {rc}:\n{tail}")


def prepare(cfg: Config, url: str) -> Path:
    """Download, transcribe, and pull chat and frames. Returns the work dir."""
    wd = workdir(cfg, url)
    wd.mkdir(parents=True, exist_ok=True)
    scripts = cfg.skill_dir / "scripts"

    if not (wd / "meeting.wav").exists() and not (wd / "transcript.srt").exists():
        _run(["sh", str(scripts / "fetch.sh"), url, str(wd)], wd, timeout=2400)

    if not (wd / "transcript.srt").exists():
        _run(
            [sys.executable, str(scripts / "transcribe.py"), "meeting.wav", "large-v3-turbo", "transcript"],
            wd,
            env={"HF_HOME": str(cfg.data_dir / "hf")},
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
            "DRY RUN: do not create, update or upload anything on the wiki. Write the finished summary "
            "body to `summary.md` in the work directory, and put that file's path in `wiki_url`."
            if cfg.dry_run
            else "Publish the summary to the wiki as the skill's ASPEN-NET output reference describes."
        ),
    )
    p = wd / "prompt.md"
    p.write_text(text)
    return p


def summarise(cfg: Config, url: str, series: str, wd: Path) -> dict:
    """Runs the agent and returns its result.json."""
    (wd / "result.json").unlink(missing_ok=True)
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
