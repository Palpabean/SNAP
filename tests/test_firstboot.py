"""Run snap-firstboot.sh against a fake root and a fake USB stick."""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "snaplab" / "stages" / "snap" / "firstboot.sh"


def make_stick(path: Path, files: dict[str, str]) -> Path:
    lines = []
    for name, content in files.items():
        f = path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
        lines.append(f"{hashlib.sha256(content.encode()).hexdigest()}  {name}\n")
    (path / "payload.sha256").write_text("".join(lines))
    return path


@pytest.fixture
def stick(tmp_path):
    return make_stick(tmp_path / "usb", {"snap.yaml": "version: 1\n", "bin/snap": "#!/bin/sh\n"})


def run(root: Path, source: Path):
    env = {**os.environ, "SNAP_ROOT": str(root), "SNAP_SOURCE": str(source)}
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)


def test_handoff(tmp_path, stick):
    root = tmp_path / "root"
    result = run(root, stick)
    assert result.returncode == 0, result.stdout
    state = root / "var/lib/snap"
    assert (state / "snap.yaml").read_text() == "version: 1\n"
    assert (state / "bin/snap").exists()
    assert (state / "handoff.done").exists()
    assert not (tmp_path / "root/var/lib/snap.partial").exists()
    assert oct(state.stat().st_mode & 0o077) == "0o0"
    unit = (root / "etc/systemd/system/snap-resume.service").read_text()
    assert "ExecStart=/var/lib/snap/bin/snap resume" in unit
    assert os.readlink(root / "usr/local/bin/snap") == "/var/lib/snap/bin/snap"
    assert "you can remove the USB now" in result.stdout
    assert "you can remove the USB now" in (root / "var/log/snap/firstboot.log").read_text()


def test_second_run_does_nothing(tmp_path, stick):
    root = tmp_path / "root"
    assert run(root, stick).returncode == 0
    (stick / "snap.yaml").write_text("changed\n")
    result = run(root, stick)
    assert result.returncode == 0
    assert "already complete" in result.stdout
    assert (root / "var/lib/snap/snap.yaml").read_text() == "version: 1\n"


def test_damaged_stick_is_refused(tmp_path, stick):
    (stick / "bin/snap").write_text("corrupted\n")
    root = tmp_path / "root"
    result = run(root, stick)
    assert result.returncode == 1
    assert "do not match their checksums" in result.stdout
    assert not (root / "var/lib/snap/handoff.done").exists()
    assert not (root / "etc/systemd/system/snap-resume.service").exists()


def test_missing_manifest_is_refused(tmp_path, stick):
    (stick / "payload.sha256").unlink()
    result = run(tmp_path / "root", stick)
    assert result.returncode == 1
    assert "payload.sha256 is missing" in result.stdout


def test_retry_after_failure_succeeds(tmp_path, stick):
    root = tmp_path / "root"
    good = (stick / "bin/snap").read_text()
    (stick / "bin/snap").write_text("corrupted\n")
    assert run(root, stick).returncode == 1
    (stick / "bin/snap").write_text(good)
    assert run(root, stick).returncode == 0
    assert (root / "var/lib/snap/handoff.done").exists()
