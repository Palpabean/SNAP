import copy
import tomllib
from pathlib import Path

import pytest
import yaml

from snaplab import cli
from snaplab.core import config
from snaplab.stages.snap import answer

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples" / "snap.example.yaml"
STATIC = ROOT / "tests" / "fixtures" / "static.yaml"


@pytest.fixture
def cfg():
    return copy.deepcopy(config.load(EXAMPLE))


def parsed(cfg):
    return tomllib.loads(answer.render(cfg))


def test_fixture_is_valid():
    config.load(STATIC)


def test_global(cfg):
    g = parsed(cfg)["global"]
    assert g == {
        "keyboard": "en-us",
        "country": "us",
        "fqdn": "pve.lab.example",
        "mailto": "admin@lab.example",
        "timezone": "America/New_York",
        "root-password-hashed": cfg["host"]["root_password_hash"],
    }


def test_no_plaintext_password_key(cfg):
    assert "root-password" not in parsed(cfg)["global"]


def test_dhcp_network(cfg):
    assert parsed(cfg)["network"] == {"source": "from-dhcp"}


def test_static_network():
    net = parsed(config.load(STATIC))["network"]
    assert net == {
        "source": "from-answer",
        "cidr": "192.168.1.10/24",
        "gateway": "192.168.1.1",
        "dns": "192.168.1.1",
        "filter": {"ID_NET_NAME_MAC": "*001122aabbcc"},
    }


def test_disk_by_serial(cfg):
    assert parsed(cfg)["disk-setup"] == {"filesystem": "ext4", "filter": {"ID_SERIAL_SHORT": "S4EWNX0R123456"}}


def test_disk_by_model():
    disk = parsed(config.load(STATIC))["disk-setup"]
    assert disk == {"filesystem": "xfs", "filter": {"ID_MODEL": "Samsung_SSD_870_EVO_1TB"}}


def test_filesystem_defaults_to_ext4(cfg):
    del cfg["host"]["storage"]
    assert parsed(cfg)["disk-setup"]["filesystem"] == "ext4"


def test_first_boot_hook(cfg):
    assert parsed(cfg)["first-boot"] == {"source": "from-iso", "ordering": "fully-up"}


def test_strings_are_escaped(cfg):
    cfg["host"]["target_disk"] = {"model": 'Odd "quoted" \\ model'}
    assert parsed(cfg)["disk-setup"]["filter"]["ID_MODEL"] == 'Odd "quoted" \\ model'


def test_cli_writes_file(tmp_path):
    out = tmp_path / "answer.toml"
    assert cli.main(["answer", str(EXAMPLE), "-o", str(out)]) == 0
    assert tomllib.loads(out.read_text())["global"]["fqdn"] == "pve.lab.example"


def test_cli_refuses_invalid_config(tmp_path, cfg):
    cfg["host"]["root_password_hash"] = "hunter2"
    bad = tmp_path / "snap.yaml"
    bad.write_text(yaml.safe_dump(cfg))
    out = tmp_path / "answer.toml"
    assert cli.main(["answer", str(bad), "-o", str(out)]) == 1
    assert not out.exists()
