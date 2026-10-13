"""The setup wizard's screens in a real browser (Chromium through Playwright).

The page is the one that goes on the stick; the window (snap-wizard.pl) is
replaced by window.SNAP_BRIDGE, which records what the page asks for.
Screenshots of each screen go to $SNAPLAB_SCREENSHOTS when that is set.
"""

import json
import os
from pathlib import Path

import pytest

from snaplab.core import config
from snaplab.delivery.usb import wizard_files

sync_api = pytest.importorskip("playwright.sync_api")

DISKS = [
    {"name": "nvme0n1", "model": "Samsung SSD 980", "gb": 512},
    {"name": "sda", "model": "WDC WD20EZRZ", "gb": 2000},
]
INTERFACES = [
    {"name": "enp1s0", "mac": "bc:24:11:00:00:01", "carrier": True},
    {"name": "enp2s0", "mac": "bc:24:11:00:00:02", "carrier": False},
]
ZONES = ["UTC", "America/New_York", "Europe/Berlin"]


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    folder = tmp_path_factory.mktemp("wizard")
    for name, content in wizard_files.files(config.load()).items():
        (folder / name).write_bytes(content)
    return folder


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        # A Chromium already on the machine (SNAPLAB_CHROMIUM) or Playwright's own.
        path = os.environ.get("SNAPLAB_CHROMIUM")
        try:
            b = p.chromium.launch(executable_path=path) if path else p.chromium.launch()
        except Exception as e:  # noqa: BLE001 - no browser on this machine
            pytest.skip(f"no Chromium for Playwright: {e}")
        yield b
        b.close()


def open_wizard(browser, site, checks=None, auto=False, size=(1024, 768)):
    page = browser.new_page(viewport={"width": size[0], "height": size[1]})
    data = {
        "config": json.loads((site / "config.json").read_text()),
        "disks": DISKS,
        "interfaces": INTERFACES,
        "timezones": ZONES,
        "checks": checks or [{"level": "pass", "text": "Internal disk found"}],
        "auto": auto,
    }
    page.add_init_script(
        "window.SNAP_SENT = []; window.SNAP_BRIDGE = (a, p) => window.SNAP_SENT.push([a, p]);"
        f"window.SNAP_TEST_DATA = {json.dumps(data)};"
    )
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto((site / "index.html").as_uri())
    page.errors = errors
    return page


def shot(page, name):
    folder = os.environ.get("SNAPLAB_SCREENSHOTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / f"{name}.png"))


def screen(page):
    return page.evaluate("SNAP.state().screen")


def click(page, text):
    page.get_by_role("button", name=text).click()


def test_next_all_the_way_installs_the_built_in_lab(browser, site):
    page = open_wizard(browser, site)
    assert screen(page) == "host"
    assert page.locator(".logo .spark").count() > 0  # the logo, with its blue spark
    shot(page, "1-host")
    password = page.input_value("#h-password")
    assert len(password) == 19 and password.count("-") == 3
    click(page, "Next: Network")
    shot(page, "2-network")
    click(page, "Next: Machines")
    shot(page, "3-machines")
    click(page, "Next: Firewall")
    shot(page, "4-firewall")
    click(page, "Next: Review")
    shot(page, "5-review")
    click(page, "Start installation")
    assert screen(page) == "review"  # the disk box is not ticked yet
    assert "Tick the box" in page.inner_text("main")
    page.check("#understood")
    click(page, "Start installation")
    assert screen(page) == "installing"
    shot(page, "6-installing")
    action, payload = page.evaluate("SNAP_SENT[SNAP_SENT.length - 1]")
    assert action == "install"
    assert payload["disk"] == "nvme0n1" and payload["password"] == password
    assert payload["host"] == {
        "hostname": "snap", "domain": "lab.internal", "timezone": "UTC", "keyboard": "en-us", "country": "us",
        "management": {"mode": "dhcp"},
    }  # fmt: skip
    assert [n["name"] for n in payload["networks"]] == ["lab", "services"]
    assert payload["networks"][0]["rules"][0]["ports"] == [22, 443]
    assert payload["machines"]["count"] == 2 and payload["machines"]["memory_mb"] == 2048
    # What the wizard sends must be a valid snap.yaml (minus the password and disk).
    cfg = {"version": 1, **{k: payload[k] for k in ("host", "networks", "machines")}}
    cfg["host"] = {**cfg["host"], "email": "admin@lab.internal", "management": {"mode": "dhcp"}}
    assert config.validate(cfg) == []
    # Progress from the window moves the bar; the end shows the restart.
    page.evaluate("SNAP.progress(42.5, 'Copying the system to the disk', 'progress  42.5 % - extracting')")
    assert page.inner_text("#p-pct") == "43%" and "Copying" in page.inner_text("#p-text")
    page.evaluate("SNAP.installDone()")
    assert "Proxmox is installed" in page.inner_text("main")
    shot(page, "8-done")
    assert page.errors == []


def test_mistakes_are_caught_before_moving_on(browser, site):
    page = open_wizard(browser, site)
    page.fill("#h-hostname", "My Lab!")
    click(page, "Next: Network")
    assert screen(page) == "host" and "Lower-case letters" in page.inner_text("main")
    page.fill("#h-hostname", "lab1")
    page.select_option("#h-mode", "static")
    click(page, "Next: Network")
    assert "Like 192.168.1.50/24" in page.inner_text("main")
    page.fill("#h-address", "192.168.1.50/24")
    page.fill("#h-gateway", "192.168.1.1")
    page.fill("#h-dns", "192.168.1.1")
    click(page, "Next: Network")
    assert screen(page) == "network"
    payload = page.evaluate("SNAP.state().payload")
    assert payload["host"]["management"] == {
        "mode": "static",
        "address": "192.168.1.50/24",
        "gateway": "192.168.1.1",
        "dns": ["192.168.1.1"],
        "iface": "enp1s0",
    }
    assert page.errors == []


def test_networks_can_be_added_edited_and_checked(browser, site):
    page = open_wizard(browser, site)
    click(page, "Next: Network")
    click(page, "+ Add another network")
    shot(page, "2b-add-network")
    assert page.input_value("#n-cidr") == "10.10.30.0/24"
    page.fill("#n-name", "dmz")
    page.fill("#n-cidr", "10.10.10.0/24")  # clashes with "lab"
    click(page, "Save network")
    assert "Overlaps the lab network" in page.inner_text("main")
    page.fill("#n-cidr", "10.10.30.0/24")
    click(page, "Save network")
    assert [n["name"] for n in page.evaluate("SNAP.state().payload.networks")] == ["lab", "services", "dmz"]
    # Renaming a network follows through to the machines and the rules.
    page.get_by_role("button", name="Edit").first.click()
    page.fill("#n-name", "office")
    click(page, "Save network")
    payload = page.evaluate("SNAP.state().payload")
    assert payload["machines"]["network"] == "office"
    click(page, "Next: Machines")
    page.fill("#m-count", "150")
    click(page, "Next: Firewall")
    assert "From 0 to 50" in page.inner_text("main")
    page.fill("#m-count", "5")
    click(page, "Next: Firewall")
    assert screen(page) == "firewall"
    page.get_by_role("button", name="Open The internet and the other lab networks.").nth(2).click()
    click(page, "+ Add rule")
    page.select_option("#r-from", "dmz")
    page.select_option("#r-to", "office")
    page.select_option("#r-protocol", "udp")
    page.fill("#r-ports", "53")
    click(page, "Add rule")
    shot(page, "4b-rules")
    networks = page.evaluate("SNAP.state().payload.networks")
    assert networks[2]["policy"] == "open"
    assert networks[2]["rules"] == [{"action": "allow", "to": "office", "protocol": "udp", "ports": [53]}]
    assert page.errors == []


def test_the_second_disk_can_be_chosen(browser, site):
    page = open_wizard(browser, site)
    for label in ("Next: Network", "Next: Machines", "Next: Firewall", "Next: Review"):
        click(page, label)
    page.check("#disk-sda")
    page.check("#understood")
    click(page, "Start installation")
    assert page.evaluate("SNAP_SENT[SNAP_SENT.length - 1][1].disk") == "sda"


def test_hardware_problems_block_setup(browser, site):
    checks = [
        {"level": "pass", "text": "Internal disk found"},
        {"level": "fix", "text": "Virtualization is turned off."},
    ]
    page = open_wizard(browser, site, checks=checks)
    assert screen(page) == "check"
    shot(page, "0-check")
    click(page, "Check again")
    assert page.evaluate("SNAP_SENT") == [["recheck", {}]]


def test_failed_install_can_be_retried(browser, site):
    page = open_wizard(browser, site)
    for label in ("Next: Network", "Next: Machines", "Next: Firewall", "Next: Review"):
        click(page, label)
    page.check("#understood")
    click(page, "Start installation")
    page.evaluate("SNAP.installFailed('disk in disk-selection not found')")
    shot(page, "7-failed")
    assert "disk in disk-selection not found" in page.inner_text("main")
    click(page, "Try again")
    assert screen(page) == "installing"
    assert [a for a, _ in page.evaluate("SNAP_SENT")].count("install") == 2


def test_unattended_mode_installs_by_itself(browser, site):
    page = open_wizard(browser, site, auto=True)
    page.wait_for_function("SNAP.state().screen === 'installing'", timeout=20000)
    assert page.evaluate("SNAP_SENT[SNAP_SENT.length - 1][0]") == "install"


DARK = "rgb(23, 34, 46)"  # the boxes' background, #17222E


@pytest.mark.parametrize("size", [(1024, 768), (1920, 1080), (2560, 1440)])
def test_wide_screens_keep_one_centred_column(browser, site, size):
    page = open_wizard(browser, site, size=size)
    width = size[0]
    for name in ("Next: Network", "Next: Machines", "Next: Firewall", "Next: Review"):
        shot(page, f"wide-{width}-{screen(page)}")
        click(page, name)
    shot(page, f"wide-{width}-review")
    box = lambda sel: page.locator(sel).first.bounding_box()  # noqa: E731
    title, start = box("h1"), box("nav button.p")
    left, right = title["x"], width - (start["x"] + start["width"])
    assert abs(left - right) < 4  # centred: the same margin on both sides
    font = page.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)")
    assert width - left - right <= 80 * font + 1  # at most 80 rem wide, not edge to edge
    assert 15 <= font <= 22 and (font > 15) == (width > 1024)  # bigger text on bigger screens
    steps = page.eval_on_selector_all(".step, .brand span", "els => els.map(e => e.getBoundingClientRect().height)")
    assert max(steps) < 2.5 * font  # the header's words stay on one line each, even at 1024 wide
    assert not page.errors


def test_controls_are_dark_not_the_window_theme(browser, site):
    page = open_wizard(browser, site)
    looks = lambda sel: page.eval_on_selector_all(  # noqa: E731
        sel, "els => els.map(e => [getComputedStyle(e).backgroundColor, getComputedStyle(e).color])"
    )
    for background, colour in looks("select") + looks("select option") + looks("input[type=text]"):
        assert background == DARK and colour == "rgb(230, 237, 243)"
    assert page.evaluate("getComputedStyle(document.documentElement).colorScheme") == "dark"
    click(page, "Next: Network")
    click(page, "Next: Machines")
    click(page, "Next: Firewall")
    click(page, "Next: Review")
    for appearance, background in page.eval_on_selector_all(
        "input[type=radio], input[type=checkbox]",
        "els => els.map(e => [getComputedStyle(e).appearance, getComputedStyle(e).backgroundColor])",
    ):
        assert appearance == "none" and background != "rgb(255, 255, 255)"
