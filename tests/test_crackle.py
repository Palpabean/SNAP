import json
import os
import stat
import zipfile

import pytest
from fake_proxmox import FakeProxmox

from snaplab import hostcli
from snaplab.core.engine import PROGRESS_DIR, Context, Engine
from snaplab.stages.crackle import stage as crackle
from snaplab.stages.crackle import tools

IMAGE = f"/var/lib/snap/{crackle.UBUNTU_IMAGE}"


COLLECTIONS = [
    ("oxlorg.opnsense", "26.1.11"),
    ("community.general", "13.5.0"),
    ("community.library_inventory_filtering_v1", "1.1.5"),
]


def stock_payload(host):
    """What the stick brings: the image, tool archives, Debian packages and tools.json."""
    host.write(IMAGE, "fake qcow2")
    payload = host.path("/var/lib/snap")
    (payload / "tools").mkdir(parents=True)
    with zipfile.ZipFile(payload / "tools/opentofu.zip", "w") as z:
        z.writestr("tofu", "#!/bin/sh\n")
        z.writestr("LICENSE", "MPL-2.0\n")
    with zipfile.ZipFile(payload / "tools/provider-proxmox.zip", "w") as z:
        z.writestr("terraform-provider-proxmox_v0.116.0", "binary")
    host.write("/var/lib/snap/debs/Packages", "Package: ansible-core\n")
    collections = []
    for name, version in COLLECTIONS:
        file = f"collections/{name.replace('.', '-')}-{version}.tar.gz"
        host.write(f"/var/lib/snap/{file}", "tar")
        collections.append({"name": name, "version": version, "file": file})
    tools_json = {
        "opentofu": {"version": "1.13.1", "file": "tools/opentofu.zip"},
        "provider_proxmox": {
            "source": "registry.opentofu.org/bpg/proxmox",
            "version": "0.116.0",
            "file": "tools/provider-proxmox.zip",
        },
        "ansible_collections": collections,
        "debian_packages": {"install": ["ansible-core", "python3-httpx"], "versions": {}},
    }
    host.write("/var/lib/snap/tools.json", json.dumps(tools_json))


@pytest.fixture
def pve(tmp_path):
    host = FakeProxmox(tmp_path)
    stock_payload(host)
    return host


def engine(host, messages=None):
    say = messages.append if messages is not None else (lambda _: None)
    return Engine([crackle.STAGE], Context(host=host, cfg={}, node="pve", say=say))


def test_crackle_sets_up_a_fresh_host(pve):
    messages = []
    assert engine(pve, messages).resume()

    assert pve.exists(f"{PROGRESS_DIR}/crackle.done")
    assert "SNAP: CRACKLE (host setup) done" in messages
    # Package sources: no-subscription added, enterprise repositories disabled, Debian untouched.
    nosub = pve.read(crackle.NO_SUBSCRIPTION)
    assert "Components: pve-no-subscription" in nosub and "Suites: trixie" in nosub
    assert "Enabled: no" in pve.read("/etc/apt/sources.list.d/pve-enterprise.sources")
    assert "Enabled: no" in pve.read("/etc/apt/sources.list.d/ceph.sources")
    assert "Enabled" not in pve.read("/etc/apt/sources.list.d/debian.sources")
    # Lab bridge created and applied; management bridge untouched.
    assert pve.bridges == {"vmbr0", "vmbr1"}
    # Storage keeps its defaults and gains snippets and imports.
    assert pve.local_content == {"iso", "vztmpl", "backup", "snippets", "import"}
    # Template from the shipped image, on the lab bridge.
    vm = pve.vms[crackle.UBUNTU_VMID]
    assert vm["template"] == "1" and vm["name"] == crackle.UBUNTU_NAME
    assert f"import-from=/var/lib/snap/{crackle.UBUNTU_IMAGE}" in vm["scsi0"]
    assert vm["net0"] == "virtio,bridge=vmbr1"


def test_second_run_changes_nothing(pve):
    assert engine(pve).resume()
    pve.calls.clear()
    assert engine(pve).resume()
    assert pve.calls == []  # the stage is marked done, so it is not even checked


def test_rerun_after_marker_loss_only_observes(pve):
    assert engine(pve).resume()
    pve.path(f"{PROGRESS_DIR}/crackle.done").unlink()
    pve.calls.clear()
    messages = []
    assert engine(pve, messages).resume()
    changing = [c for c in pve.calls if c[0] in ("qm", "pvesm") and c[1] in ("create", "set", "template", "destroy")]
    assert changing == []
    assert sum("already done" in m for m in messages) == len(crackle.STAGE.steps)


def test_failure_is_recorded_and_resume_continues_from_there(pve):
    pve.fail["template"] = "storage full"
    messages = []
    assert not engine(pve, messages).resume()
    assert not pve.exists(f"{PROGRESS_DIR}/crackle.done")
    state = json.loads(pve.read("/var/lib/snap/state.json"))
    steps = state["stages"]["crackle"]["steps"]
    assert steps["lab-bridge"]["status"] == "done"
    assert steps["ubuntu-template"]["status"] == "failed"
    assert "storage full" in steps["ubuntu-template"]["error"]
    assert any("Create the Ubuntu 24.04 VM template: " in m and "storage full" in m for m in messages)

    # The half-made VM from the failed attempt is replaced on resume.
    del pve.fail["template"]
    assert engine(pve).resume()
    assert pve.vms[crackle.UBUNTU_VMID]["template"] == "1"
    assert ("qm", "destroy", "9000", "--purge", "1", "--destroy-unreferenced-disks", "1") in pve.calls


def test_someone_elses_vm_is_never_destroyed(pve):
    pve.vms[crackle.UBUNTU_VMID] = {"name": "my-important-vm"}
    assert not engine(pve).resume()
    assert pve.vms[crackle.UBUNTU_VMID] == {"name": "my-important-vm"}
    state = json.loads(pve.read("/var/lib/snap/state.json"))
    assert "is not SNAP's" in state["stages"]["crackle"]["steps"]["ubuntu-template"]["error"]


def test_missing_image_fails_preflight_before_changing_anything(pve):
    pve.path(IMAGE).unlink()
    assert not engine(pve).resume()
    assert not pve.exists(crackle.NO_SUBSCRIPTION)
    state = json.loads(pve.read("/var/lib/snap/state.json"))
    assert "Ubuntu image is missing" in state["stages"]["crackle"]["error"]


def test_waits_for_api(pve, monkeypatch):
    pve.api_up = False
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            pve.api_up = True

    monkeypatch.setattr(crackle.time, "sleep", sleep)
    assert engine(pve).resume()
    assert len(sleeps) == 3


def test_api_timeout_is_a_clear_failure(pve, monkeypatch):
    pve.api_up = False
    monkeypatch.setattr(crackle.time, "sleep", lambda _: None)
    ctx = Context(host=pve, cfg={}, node="pve")
    with pytest.raises(RuntimeError, match="did not answer"):
        crackle.wait_for_api(ctx, timeout=0)


def test_legacy_list_sources_are_commented_out(pve):
    pve.path("/etc/apt/sources.list.d/pve-enterprise.sources").unlink()
    pve.write(
        "/etc/apt/sources.list.d/pve-enterprise.list",
        "deb https://enterprise.proxmox.com/debian/pve trixie pve-enterprise\n",
    )
    assert engine(pve).resume()
    assert pve.read("/etc/apt/sources.list.d/pve-enterprise.list").startswith("# deb https://enterprise")


def test_status_and_cli(pve, capsys, monkeypatch):
    monkeypatch.setattr(hostcli, "console", lambda m: print(m))
    pve.write("/var/lib/snap/snap.json", "{}")
    assert hostcli.main(["status"], host=pve) == 0
    assert "CRACKLE (host setup): pending" in capsys.readouterr().out
    assert hostcli.main(["resume"], host=pve) == 0
    out = capsys.readouterr().out
    assert "SNAP: CRACKLE (host setup) done" in out and "POP" in out
    assert hostcli.main(["status"], host=pve) == 0
    assert "  - Create the Ubuntu 24.04 VM template: done" in capsys.readouterr().out
    assert hostcli.main(["logs"], host=pve) == 0
    assert "CRACKLE (host setup) done" in capsys.readouterr().out


def test_automation_tools_are_installed_offline(pve):
    assert engine(pve).resume()
    # OpenTofu, with the provider as a local mirror and a config that uses it.
    assert pve.exists(tools.TOFU) and os.access(pve.path(tools.TOFU), os.X_OK)
    mirror = "/usr/local/share/snap/tofu-providers/registry.opentofu.org/bpg/proxmox/0.116.0/linux_amd64"
    assert pve.exists(f"{mirror}/terraform-provider-proxmox_v0.116.0")
    rc = pve.read(tools.TOFU_CONFIG)
    assert 'include = ["registry.opentofu.org/bpg/proxmox"]' in rc and "filesystem_mirror" in rc
    # Ansible from the stick's packages only, then the collections.
    assert pve.debs == {"ansible-core", "python3-httpx"}
    assert "file:/var/lib/snap/debs ./" in pve.read("/var/lib/snap/debs/snap-local.list")
    assert pve.collections == dict(COLLECTIONS)


def test_api_token_is_least_privilege_and_kept_secret(pve):
    assert engine(pve).resume()
    privileges = pve.roles[tools.API_ROLE].split(",")
    assert "VM.Clone" in privileges and not any(p.startswith(("Sys.Modify", "Permissions")) for p in privileges)
    assert ("/", tools.API_USER, tools.API_ROLE) in pve.acls
    secret = pve.read(tools.API_SECRET).strip()
    assert secret == f"snap@pve!tofu={pve.tokens['snap@pve!tofu']}"
    assert stat.S_IMODE(pve.path(tools.API_SECRET).stat().st_mode) == 0o600
    assert stat.S_IMODE(pve.path("/var/lib/snap/secrets").stat().st_mode) == 0o700


def test_lost_token_secret_is_replaced(pve):
    assert engine(pve).resume()
    old = pve.tokens["snap@pve!tofu"]
    pve.path(tools.API_SECRET).unlink()
    pve.path(f"{PROGRESS_DIR}/crackle.done").unlink()
    assert engine(pve).resume()
    assert pve.tokens["snap@pve!tofu"] != old
    assert pve.read(tools.API_SECRET).strip().endswith(pve.tokens["snap@pve!tofu"])


def test_missing_tool_archives_fail_preflight(pve):
    pve.path("/var/lib/snap/tools/opentofu.zip").unlink()
    assert not engine(pve).resume()
    state = json.loads(pve.read("/var/lib/snap/state.json"))
    assert "tools/opentofu.zip is missing" in state["stages"]["crackle"]["error"]
