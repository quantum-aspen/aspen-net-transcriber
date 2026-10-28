"""Runs a step's subprocess, teeing its output to a log file and to our stdout.

The file keeps the job's own log (its tail goes into error messages); stdout
puts every line in the container log, tagged with the job, for log shipping.
"""

import logging
import subprocess
import threading
from pathlib import Path

log = logging.getLogger("transcriber.job")


def run(cmd: list[str], *, cwd: Path, log_file: Path, tag: str, env: dict | None = None, timeout: int) -> int:
    """Returns the exit code. Raises subprocess.TimeoutExpired after timeout seconds.
    The child is killed if we're interrupted (e.g. SIGTERM) while it runs."""
    timed_out = threading.Event()
    with (
        log_file.open("ab") as out,
        subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as p,
    ):

        def kill() -> None:
            timed_out.set()
            p.kill()

        timer = threading.Timer(timeout, kill)
        timer.start()
        try:
            for line in p.stdout:
                out.write(line)
                out.flush()
                # whisper and yt-dlp redraw progress with \r; keep the last frame
                text = line.decode(errors="replace").rstrip().rsplit("\r", 1)[-1]
                if text:
                    log.info("[%s] %s", tag, text)
            rc = p.wait()
        except BaseException:
            p.kill()
            raise
        finally:
            timer.cancel()
    if timed_out.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout)
    return rc
