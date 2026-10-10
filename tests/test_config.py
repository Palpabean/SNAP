import copy
from pathlib import Path

import pytest
import yaml

from snaplab import cli
from snaplab.core import config

EXAMPLE = Path(__file__).parent.parent / "examples" / "snap.example.yaml"


@pytest.fixture
def cfg():
    return copy.deepcopy(yaml.safe_load(EXAMPLE.read_text()))


def errors_for(data):
    errors = config.validate(data)
    assert errors, "expected the config to be rejected"
    return "\n".join(errors)


def test_example_is_valid():
    assert config.load(EXAMPLE)["version"] == 1


def test_minimal_config_is_valid(cfg):
    del cfg["options"], cfg["host"]["storage"]
    cfg["networks"] = [{"name": "lab", "cidr": "10.0.0.0/24", "router": "10.0.0.1", "policy": "isolated"}]
    cfg["machines"]["mode"] = "templates-only"
    assert config.validate(cfg) == []


# --- structure (JSON Schema) ---


def test_unknown_version(cfg):
    cfg["version"] = 2
    assert "version" in errors_for(cfg)


def test_unknown_key_is_rejected(cfg):
    cfg["host"]["password"] = "hunter2"
    assert "Additional properties" in errors_for(cfg)


@pytest.mark.parametrize("value", ["hunter2", "$1$md5salt$abc", ""])
def test_plaintext_or_weak_password_is_rejected(cfg, value):
    cfg["host"]["root_password_hash"] = value
    assert "host.root_password_hash" in errors_for(cfg)


def test_target_disk_needs_exactly_one_selector(cfg):
    cfg["host"]["target_disk"] = {}
    assert "host.target_disk" in errors_for(cfg)
    cfg["host"]["target_disk"] = {"serial": "ABCD1234", "model": "Samsung"}
    assert "host.target_disk" in errors_for(cfg)


def test_static_management_needs_address(cfg):
    cfg["host"]["management"] = {"mode": "static"}
    assert "host.management" in errors_for(cfg)


def test_dhcp_management_rejects_static_fields(cfg):
    cfg["host"]["management"]["address"] = "192.168.1.10/24"
    assert "host.management" in errors_for(cfg)


def test_invalid_ipv4(cfg):
    cfg["networks"][0]["router"] = "10.10.10.300"
    assert "networks[0].router" in errors_for(cfg)


def test_machines_need_a_credential(cfg):
    del cfg["machines"]["password_hash"]
    assert "machines" in errors_for(cfg)
    cfg["machines"]["ssh_keys"] = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample user@laptop"]
    assert config.validate(cfg) == []


def test_root_username_is_rejected(cfg):
    cfg["machines"]["username"] = "root"
    assert "machines.username" in errors_for(cfg)


# --- cross-field checks ---


def test_static_gateway_outside_subnet(cfg):
    cfg["host"]["management"] = {
        "mode": "static",
        "mac": "00:11:22:33:44:55",
        "address": "192.168.1.10/24",
        "gateway": "192.168.2.1",
        "dns": ["192.168.1.1"],
    }
    assert "host.management.gateway" in errors_for(cfg)


def test_cidr_with_host_bits(cfg):
    cfg["networks"][0]["cidr"] = "10.10.10.5/24"
    assert "networks[0].cidr" in errors_for(cfg)


def test_router_outside_network(cfg):
    cfg["networks"][0]["router"] = "10.10.99.1"
    assert "networks[0].router" in errors_for(cfg)


def test_router_is_broadcast(cfg):
    cfg["networks"][0]["router"] = "10.10.10.255"
    assert "networks[0].router" in errors_for(cfg)


def test_dhcp_range_outside_network(cfg):
    cfg["networks"][0]["dhcp"]["end"] = "10.10.11.10"
    assert "networks[0].dhcp.end" in errors_for(cfg)


def test_dhcp_range_reversed(cfg):
    cfg["networks"][0]["dhcp"].update(start="10.10.10.200", end="10.10.10.100")
    assert "is after end" in errors_for(cfg)


def test_dhcp_range_contains_router(cfg):
    cfg["networks"][0]["dhcp"]["start"] = "10.10.10.1"
    assert "contains the router" in errors_for(cfg)


def test_duplicate_network_names(cfg):
    cfg["networks"][1]["name"] = "lab"
    assert "more than one network" in errors_for(cfg)


def test_duplicate_vlans(cfg):
    cfg["networks"][1]["vlan"] = 10
    assert "networks[1].vlan" in errors_for(cfg)


def test_overlapping_networks(cfg):
    cfg["networks"][1].update(cidr="10.10.0.0/16", router="10.10.20.1")
    assert "overlaps" in errors_for(cfg)


def test_only_one_untagged_network(cfg):
    del cfg["networks"][0]["vlan"], cfg["networks"][1]["vlan"]
    assert "only one network can be untagged" in errors_for(cfg)


def test_rule_to_unknown_target(cfg):
    cfg["networks"][0]["rules"][0]["to"] = "nowhere"
    assert "networks[0].rules[0].to" in errors_for(cfg)


def test_rule_to_itself(cfg):
    cfg["networks"][0]["rules"][0]["to"] = "lab"
    assert "rule to itself" in errors_for(cfg)


def test_rule_ports_need_tcp_or_udp(cfg):
    cfg["networks"][0]["rules"][0]["protocol"] = "icmp"
    assert "networks[0].rules[0].ports" in errors_for(cfg)


def test_machines_on_unknown_network(cfg):
    cfg["machines"]["network"] = "nowhere"
    assert "machines.network" in errors_for(cfg)


def test_machines_need_dhcp(cfg):
    cfg["networks"][0]["dhcp"] = {"enabled": False}
    assert "needs DHCP" in errors_for(cfg)


def test_templates_only_does_not_need_dhcp(cfg):
    cfg["networks"][0]["dhcp"] = {"enabled": False}
    cfg["machines"]["mode"] = "templates-only"
    assert config.validate(cfg) == []


def test_too_many_machines_for_dhcp_range(cfg):
    cfg["networks"][0]["dhcp"]["end"] = "10.10.10.101"
    assert "machines.count" in errors_for(cfg)


# --- loading and CLI ---


def test_load_reports_bad_yaml(tmp_path):
    bad = tmp_path / "snap.yaml"
    bad.write_text("host: [unclosed")
    with pytest.raises(config.ConfigError, match="not valid YAML"):
        config.load(bad)


def test_load_reports_missing_file(tmp_path):
    with pytest.raises(config.ConfigError, match="cannot read"):
        config.load(tmp_path / "missing.yaml")


def test_cli_validate(tmp_path, capsys, cfg):
    assert cli.main(["validate", str(EXAMPLE)]) == 0
    cfg["machines"]["network"] = "nowhere"
    bad = tmp_path / "snap.yaml"
    bad.write_text(yaml.safe_dump(cfg))
    assert cli.main(["validate", str(bad)]) == 1
    assert "machines.network" in capsys.readouterr().err
