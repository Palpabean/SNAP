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


def test_cd_drives():
    assert opnsense.cd_drives(DEVLIST) == ["cd0"]
    assert opnsense.cd_drives("<QEMU DVD-ROM>  at scbus1 target 0 lun 0 (cd1,pass3)") == ["cd1"]
    assert opnsense.cd_drives("<QEMU DVD-ROM 2.5+>   at scbus0 target 0 lun 0 ()") == []


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


def test_drive_not_ready_yet_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(opnsense.time, "sleep", lambda _: None)
    unnamed = "<QEMU DVD-ROM 2.5+>   at scbus0 target 0 lun 0 ()\n"
    again = unnamed + opnsense.DEVICE_PROMPT + ": "
    fake = FakeConsole(
        tmp_path,
        [
            (opnsense.IMPORTER_PROMPT + ": ...", "\r"),
            (again, "cd0\r"),
            ("No known partition layout was found for 'cd0'.\n" + again, "cd1\r"),
            ("The device could not be mounted.\nThe file /conf/config.xml could not be found.\n" + again, "cd2\r"),
            ("No known partition layout was found for 'cd2'.\n" + again, "cd3\r"),
            ("No known partition layout was found for 'cd3'.\n" + again, "cd0\r"),
            (opnsense.IMPORTED + "\n", None),
        ],
    )
    console = opnsense.Serial(fake.path, connect_timeout=5)
    assert opnsense.run_importer(console, timeout=10) == "cd0"
    assert fake.received == ["\r", "cd0\r", "cd1\r", "cd2\r", "cd3\r", "cd0\r"]


def test_no_cd_drive_leaves_the_importer(tmp_path, monkeypatch):
    monkeypatch.setattr(opnsense.time, "sleep", lambda _: None)
    missing = [
        (f"No known partition layout was found for 'cd{i}'.\n" + opnsense.DEVICE_PROMPT, f"cd{i + 1}\r")
        for i in range(3)
    ]
    fake = FakeConsole(
        tmp_path,
        [(opnsense.IMPORTER_PROMPT + ": ...", "\r"), (opnsense.DEVICE_PROMPT, "cd0\r"), *missing]
        + [("No known partition layout was found for 'cd3'.\n" + opnsense.DEVICE_PROMPT, "\r")],
    )
    console = opnsense.Serial(fake.path, connect_timeout=5)
    with pytest.raises(opnsense.ImportFailed, match="no CD drive held the config"):
        opnsense.run_importer(console, timeout=10, settle=0)
    fake.thread.join(5)
    assert fake.received[-1] == "\r"


def test_missing_prompt_times_out(tmp_path):
    fake = FakeConsole(tmp_path, [("booting...\n", None)])
    console = opnsense.Serial(fake.path, connect_timeout=5)
    with pytest.raises(TimeoutError, match="importer"):
        opnsense.run_importer(console, timeout=1)
