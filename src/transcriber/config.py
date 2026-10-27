"""Settings, all from environment variables (see .env.example)."""

import os
from dataclasses import dataclass
from pathlib import Path


def _hours(spec: str) -> tuple[int, int] | None:
    """Parses "22-6" into (22, 6). Empty means any time."""
    if not spec.strip():
        return None
    start, end = (int(x) for x in spec.split("-"))
    if not (0 <= start < 24 and 0 <= end <= 24):
        raise ValueError(f"RUN_HOURS out of range: {spec}")
    return start, end


@dataclass(frozen=True)
class Config:
    outline_url: str
    outline_api_key: str
    inbox_doc: str
    lumen_base_url: str
    lumen_model: str
    lumen_vision_model: str
    data_dir: Path
    skills_dir: Path
    poll_interval: int
    run_hours: tuple[int, int] | None
    max_attempts: int
    agent_max_turns: int
    agent_budget: int
    dry_run: bool

    @property
    def skill_dir(self) -> Path:
        return self.skills_dir / "local-meeting-transcript"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def meetings_dir(self) -> Path:
        return self.data_dir / "meetings"

    @property
    def state_db(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def heartbeat(self) -> Path:
        return self.data_dir / "heartbeat"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        e = os.environ if env is None else env
        return cls(
            outline_url=e.get("OUTLINE_URL", "https://wiki.quantum-aspen.net").rstrip("/"),
            outline_api_key=e.get("OUTLINE_API_KEY", ""),
            inbox_doc=e.get("INBOX_DOC", ""),
            lumen_base_url=e.get("LUMEN_BASE_URL", "https://lumen.ncsa.illinois.edu/v1").rstrip("/"),
            lumen_model=e.get("LUMEN_MODEL", ""),
            lumen_vision_model=e.get("LUMEN_VISION_MODEL", ""),
            data_dir=Path(e.get("TRANSCRIBER_DATA", "/opt/data/transcriber")),
            skills_dir=Path(e.get("SKILLS_DIR", "/opt/transcriber/skills")),
            poll_interval=int(e.get("POLL_INTERVAL", "300")),
            run_hours=_hours(e.get("RUN_HOURS", "")),
            max_attempts=int(e.get("MAX_ATTEMPTS", "2")),
            agent_max_turns=int(e.get("AGENT_MAX_TURNS", "90")),
            agent_budget=int(e.get("AGENT_BUDGET_SECONDS", "2700")),
            dry_run=e.get("DRY_RUN", "") not in ("", "0", "false"),
        )

    def missing(self, *, agent: bool = True, inbox: bool = True) -> list[str]:
        """Names of required settings that aren't set."""
        need = {}
        if inbox:
            need |= {"OUTLINE_API_KEY": self.outline_api_key, "INBOX_DOC": self.inbox_doc}
        if agent:
            need |= {"LUMEN_MODEL": self.lumen_model, "LUMEN_API_KEY": os.environ.get("LUMEN_API_KEY", "")}
            if not self.dry_run:
                need |= {"OUTLINE_API_KEY": self.outline_api_key}
        return [k for k, v in need.items() if not v]
