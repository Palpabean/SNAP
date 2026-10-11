import socket
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from snaplab.core import config
from snaplab.stages.pop import opnsense

EXAMPLE = Path(__file__).parent.parent / "examples" / "snap.example.yaml"


@pytest.fixture
def cfg():
    return config.load(EXAMPLE)


def test_interfaces_follow_the_vm_network_cards(cfg):
    found = [(i.key, i.device, i.descr) for i in opnsense.interfaces(cfg)]
    assert found == [
        ("wan", "vtnet0", "WAN"),
        ("opt1", "vtnet1", "ADMIN"),
        ("opt2", "vtnet2", "LAB"),
        ("opt3", "vtnet3", "SERVICES"),
    ]


def test_bootstrap_config(cfg):
    root = ET.fromstring(opnsense.render_config(cfg, "the-key", "$6$salt$hash"))
    user = root.find("system/user")
    assert user.findtext("password") == cfg["host"]["root_password_hash"]
    assert user.findtext("apikeys") == "the-key|$6$salt$hash"
    assert root.findtext("system/primaryconsole") == "serial"
    assert root.findtext("interfaces/wan/ipaddr") == "dhcp"
    assert root.findtext("interfaces/opt1/ipaddr") == str(opnsense.ADMIN_ROUTER)
    assert root.findtext("interfaces/opt2/ipaddr") == "10.10.10.1"
    assert root.findtext("interfaces/opt3/subnet") == "24"
    # Only the admin network gets rules here: the API, SSH and ping. The lab's own
    # policy comes later through the API.
    rules = root.findall("filter/rule")
    assert {r.findtext("interface") for r in rules} == {"opt1"}
    api = [r for r in rules if r.findtext("destination/port") == "443"]
    assert len(api) == 1 and api[0].findtext("destination/network") == "(self)"
    assert {r.findtext("destination/network") for r in rules if r.findtext("destination/port") == "22"} == {
        "opt2",
        "opt3",
    }


DEVLIST = """
<QEMU HARDDISK 2.5+>               at scbus0 target 0 lun 0 (ada0,pass0)
<QEMU DVD-ROM 2.5+>                at scbus2 target 0 lun 0 (pass1,cd0)
"""


def test_find_cdrom():
    assert opnsense.find_cdrom(DEVLIST) == "cd0"
    assert opnsense.find_cdrom("<QEMU DVD-ROM>  at scbus1 target 0 lun 0 (cd1,pass3)") == "cd1"
    assert opnsense.find_cdrom("<Virtio disk> (vtbd0)") is None


class FakeConsole:
    """A serial socket that plays a script: output, then what it expects back."""

    def __init__(self, tmp_path, script):
        self.path = str(tmp_path / "serial.sock")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(1)
        self.received = []
        self.thread = threading.Thread(target=self._play, args=(script,), daemon=True)
        self.thread.start()

    def _play(self, script):
        conn, _ = self.server.accept()
        with conn:
            for output, expect in script:
                for chunk in (output[: len(output) // 2], output[len(output) // 2 :]):  # split prompts
                    conn.sendall(chunk.encode())
                if expect is not None:
                    got = b""
                    while not got.endswith(b"\r"):
                        got += conn.recv(64)
                    self.received.append(got.decode())
            conn.recv(1)  # stay connected, like a running VM, until the client hangs up


def test_importer_is_answered(tmp_path):
    fake = FakeConsole(
        tmp_path,
        [
            ("Mounting filesystems...\n" + opnsense.IMPORTER_PROMPT + ": ...", "\r"),
            (DEVLIST + "\n" + opnsense.DEVICE_PROMPT + " (e.g. ada0) or leave blank to exit: ", "cd0\r"),
            ("Starting import for partition '/dev/cd0'.\n\n" + opnsense.IMPORTED + "\n", None),
        ],
    )
    console = opnsense.Serial(fake.path, connect_timeout=5)
    assert opnsense.run_importer(console, timeout=10) == "cd0"
    console.close()
    assert fake.received == ["\r", "cd0\r"]


def test_importer_failure_is_reported(tmp_path):
    fake = FakeConsole(
        tmp_path,
        [
            (opnsense.IMPORTER_PROMPT + ": ...", "\r"),
            (DEVLIST + opnsense.DEVICE_PROMPT + ": ", "cd0\r"),
            ("The device could not be mounted.\n", None),
        ],
    )
    console = opnsense.Serial(fake.path, connect_timeout=5)
    with pytest.raises(opnsense.ImportFailed, match="could not be mounted"):
        opnsense.run_importer(console, timeout=10)


def test_no_cd_drive_leaves_the_importer(tmp_path):
    fake = FakeConsole(
        tmp_path,
        [(opnsense.IMPORTER_PROMPT + ": ...", "\r"), ("<disk> (ada0,pass0)\n" + opnsense.DEVICE_PROMPT, "\r")],
    )
    console = opnsense.Serial(fake.path, connect_timeout=5)
    with pytest.raises(opnsense.ImportFailed, match="no CD drive"):
        opnsense.run_importer(console, timeout=10)
    fake.thread.join(5)
    assert fake.received == ["\r", "\r"]


def test_missing_prompt_times_out(tmp_path):
    fake = FakeConsole(tmp_path, [("booting...\n", None)])
    console = opnsense.Serial(fake.path, connect_timeout=5)
    with pytest.raises(TimeoutError, match="importer"):
        opnsense.run_importer(console, timeout=1)
