"""The scheduled audit leaves its output available to cron's mail delivery."""

import shlex
import subprocess
from pathlib import Path


def test_cron_warmup_output_is_available_for_mail(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / ".env").write_text("")
    uv = tmp_path / "fake-uv"
    uv.write_text("#!/bin/sh\nprintf 'audit flagged\\n'\nexit 1\n")
    uv.chmod(0o700)
    example = Path(__file__).resolve().parents[1] / "examples" / "scheduling" / "cron.txt"
    line = next(line for line in example.read_text().splitlines()
                if line and not line.startswith("#") and "warmup --strict" in line)
    command = line.split(maxsplit=5)[5].replace("/path/to/lead-engine", shlex.quote(str(tmp_path)))
    command = command.replace("/path/to/uv", shlex.quote(str(uv)))
    result = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True)
    assert result.returncode == 1 and "audit flagged" in result.stdout
