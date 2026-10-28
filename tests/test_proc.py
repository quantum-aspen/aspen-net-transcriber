import subprocess
import sys

import pytest

from transcriber import proc


def test_tees_output_to_file_and_log(tmp_path, caplog):
    caplog.set_level("INFO")
    code = "import sys; print('one'); print('50%\\r100%'); sys.exit(3)"
    rc = proc.run(
        [sys.executable, "-c", code], cwd=tmp_path, log_file=tmp_path / "x.log", tag="job1", timeout=30
    )
    assert rc == 3
    assert (tmp_path / "x.log").read_bytes() == b"one\n50%\r100%\n"
    assert [r.getMessage() for r in caplog.records] == ["[job1] one", "[job1] 100%"]


def test_timeout_kills_and_raises(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        proc.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path, log_file=tmp_path / "x.log", tag="t", timeout=1,
        )  # fmt: skip
