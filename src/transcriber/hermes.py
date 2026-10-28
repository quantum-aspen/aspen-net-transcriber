"""Hermes configuration and invocation.

The image's init seeds $HERMES_HOME/config.yaml on first boot. We merge our
keys into it on every start, so the config always matches this image and its
env, and nothing about it lives only in the data volume.
"""

import os
import subprocess
from pathlib import Path

import yaml

from . import proc
from .config import Config


def settings(cfg: Config) -> dict:
    s = {
        "model": {
            "provider": "custom",
            "base_url": cfg.lumen_base_url,
            "default": cfg.lumen_model,
            "key_env": "LUMEN_API_KEY",
        },
        "skills": {"external_dirs": [str(cfg.skills_dir)]},
        # Each run is independent: nothing should carry over between meetings.
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "auxiliary": {"title_generation": {"enabled": False}},
    }
    if cfg.lumen_vision_model:
        s["auxiliary"]["vision"] = {
            "provider": "custom",
            "base_url": cfg.lumen_base_url,
            "model": cfg.lumen_vision_model,
            "key_env": "LUMEN_API_KEY",
        }
    if not cfg.dry_run:
        s["mcp_servers"] = {
            "outline": {
                # Hermes resolves ${VAR} at connect time, so the key stays in the env.
                "url": f"{cfg.outline_url}/mcp",
                "headers": {"Authorization": "Bearer ${OUTLINE_API_KEY}"},
            }
        }
    return s


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def write_config(cfg: Config, home: Path | None = None) -> Path:
    home = home or Path(os.environ.get("HERMES_HOME", "/opt/data"))
    path = home / "config.yaml"
    current = yaml.safe_load(path.read_text()) if path.exists() else None
    merged = _merge(current or {}, settings(cfg))
    if cfg.dry_run:
        merged.get("mcp_servers", {}).pop("outline", None)
    home.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(merged, sort_keys=False))
    return path


def drop_bundled_skills() -> None:
    """The image seeds ~60 general skills (Apple Notes, iMessage, ...) on first
    boot. They'd only crowd the agent's skill index, so stop the seeding and
    remove the unmodified copies. Idempotent."""
    subprocess.run(
        ["hermes", "skills", "opt-out", "--remove", "--yes"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=False,
    )  # fmt: skip


# Only what the summary needs. Meeting content (speech, chat, screen text) is
# untrusted input to an agent running with --yolo, so no web, browser, computer
# use, delegation, cron, connectors or memory. MCP servers (Outline) come from config.yaml.
TOOLSETS = "terminal,file,vision,skills,todo"


def run(cfg: Config, prompt: Path, workdir: Path, log: Path) -> int:
    """One unattended agent run. Its output goes to log; returns the exit code."""
    cmd = [
        "hermes", "chat",
        "--query-file", str(prompt),
        "--quiet",
        "--skills", "local-meeting-transcript",
        "--toolsets", TOOLSETS,
        "--max-turns", str(cfg.agent_max_turns),
        "--run-budget", str(cfg.agent_budget),
        "--yolo",  # no one is there to approve tool calls
    ]  # fmt: skip
    env = os.environ | {
        "MEETING_UNATTENDED": "1",
        "MEETING_TOOLS_DIR": str(cfg.data_dir / "tools"),
    }
    return proc.run(cmd, cwd=workdir, log_file=log, tag=workdir.name, env=env, timeout=cfg.agent_budget + 300)
