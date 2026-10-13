"""labcheck.py: the checks the lab check (verify.yml) runs on each lab machine."""

import importlib.util
import struct
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "snaplab" / "stages" / "pop" / "ansible" / "labcheck.py"
spec = importlib.util.spec_from_file_location("labcheck", SCRIPT)
labcheck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(labcheck)


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True).returncode


def test_has_an_address_only_when_assigned():
    assert run("has", "127.0.0.1") == 0
    assert run("has", "10.255.254.253") == 1


def test_resolve():
    assert run("resolve", "localhost") == 0
    assert run("resolve", "no-such-name.invalid") == 1


def test_icmp_checksum():
    # An echo request whose checksum field is filled in sums to zero.
    header = struct.pack("!BBHHH", 8, 0, 0, labcheck.IDENT, 1) + b"SNAP lab check"
    filled = header[:2] + struct.pack("!H", labcheck.checksum(header)) + header[4:]
    assert labcheck.checksum(filled) == 0


def test_only_standard_library():
    """It runs on the lab machines, which have Python and nothing else."""
    imported = {
        line.split()[1].split(".")[0]
        for line in SCRIPT.read_text().splitlines()
        if line.startswith(("import ", "from "))
    }
    assert imported <= {"socket", "struct", "sys"}
