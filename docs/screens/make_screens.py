"""Make the pictures in docs/screens from SNAP's real screens.

    python docs/screens/make_screens.py             all of them
    python docs/screens/make_screens.py --host      only the lab computer's screens

- The boot menu: booted from a test stick in QEMU (tests/test_usb_build.py).
- The setup screens: the real page in Chromium (tests/test_wizard_ui.py).
- The lab computer's screens: drawn from `snap screen`'s own output.

Needs Pillow for the lab computer's screens; the others need what their tests need
(Playwright with Chromium; QEMU, OVMF and the disk-image tools). A picture whose
tools are missing is left as it was.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

# Test screenshot name -> picture name here, in the order a user sees them.
FROM_TESTS = {
    "boot-menu-uefi.png": "01-boot-menu.png",
    "0-check.png": "02-hardware-check.png",
    "1-host.png": "03-host.png",
    "2-network.png": "04-network.png",
    "2b-add-network.png": "05-network-add.png",
    "3-machines.png": "06-machines.png",
    "4-firewall.png": "07-firewall.png",
    "4b-rules.png": "08-firewall-rules.png",
    "5-review.png": "09-review.png",
    "6-installing.png": "10-installing.png",
    "8-done.png": "11-installed.png",
    "7-failed.png": "12-install-failed.png",
    "volatile-boot-menu-uefi.png": "16-volatile-boot-menu.png",
}
TESTS = [["tests/test_usb_build.py", "-k", "fresh"], ["tests/test_wizard_ui.py"]]

# The lab computer's console, in the setup screens' colours.
BACKGROUND = (6, 9, 13)
TEXT = (208, 215, 222)
COLOURS = {"32": (63, 185, 80), "36": (45, 212, 191), "31": (255, 107, 107), "2": (138, 155, 173), "1": (255, 255, 255)}
COLOURS["34"] = (59, 130, 246)
COLOURS["1;34"], COLOURS["1;36"] = COLOURS["34"], COLOURS["36"]  # the volatile start-up screen
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
COLS, ROWS = 100, 44


def from_tests() -> None:
    with tempfile.TemporaryDirectory() as shots:
        env = {**os.environ, "SNAPLAB_SCREENSHOTS": shots}
        for tests in TESTS:
            subprocess.run([sys.executable, "-m", "pytest", "-q", *tests], cwd=ROOT, env=env, check=False)
        for name, target in FROM_TESTS.items():
            if (Path(shots) / name).exists():
                shutil.copyfile(Path(shots) / name, HERE / target)
            else:
                print(f"not made (tools missing?): {target}")


def host_screens(mode: str = "permanent") -> dict[str, list[str]]:
    """`snap screen`'s output for each situation, on a stand-in lab computer."""
    import json

    from snaplab import hostcli, screen
    from snaplab.core import defaults
    from snaplab.core.engine import PROGRESS_DIR, STATE_FILE, Context, Engine
    from snaplab.core.host import CommandError, Host
    from snaplab.stages.bang import stage as bang

    class LabComputer(Host):
        def run(self, *cmd, input=None):
            if cmd[:2] == ("ip", "-j"):
                return json.dumps([{"ifname": "vmbr0", "addr_info": [{"family": "inet", "local": "192.168.1.50"}]}])
            raise CommandError(list(cmd), 1, "")

    pop_steps = ["addresses", "opnsense-template", "admin-network", "ssh-key", "router-config"]
    situations = {
        "13-lab-setting-up.png": (["snap", "crackle"], {"pop": ("running", pop_steps + ["router"], "router-settings")}),
        "14-lab-ready.png": (["snap", "crackle", "pop", "bang"], {}),
        "15-lab-needs-attention.png": (["snap", "crackle"], {"pop": ("failed", pop_steps, "router")}),
    }
    if mode == "volatile":
        situations = {
            "18-volatile-setting-up.png": situations["13-lab-setting-up.png"],
            "19-volatile-ready.png": situations["14-lab-ready.png"],
        }
    out = {}
    for picture, (finished, running) in situations.items():
        with tempfile.TemporaryDirectory() as root:
            host = LabComputer(root)
            for name in finished:
                host.write(f"{PROGRESS_DIR}/{name}.done", "1\n")
            state = {"stages": {}}
            for stage, (status, done, current) in running.items():
                steps = {s: {"status": "done"} for s in done} | {current: {"status": status}}
                state["stages"][stage] = {"status": status, "steps": steps}
                if status == "failed":
                    state["stages"][stage]["error"] = (
                        "Create the router and import its config: the router did not answer within 15 minutes"
                    )
            host.write(STATE_FILE, json.dumps(state))
            host.write(bang.PASSWORD_FILE, "ab3k-9xqe-7tmd-p2wr\n")
            cfg = json.loads(json.dumps(defaults.DEFAULTS))
            cfg["host"]["mode"] = mode
            engine = Engine(hostcli.STAGES, Context(host=host, cfg=cfg, node="snap"))
            out[picture] = screen.render(screen.snapshot(engine), COLS, ROWS)
    return out


def draw(lines: list[str], path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(FONT, 16)
    cell_w, cell_h = round(font.getlength("M")), 20
    image = Image.new("RGB", (cell_w * COLS + 40, cell_h * ROWS + 40), BACKGROUND)
    pen = ImageDraw.Draw(image)
    for row, line in enumerate(lines):
        col, colour = 0, TEXT
        for part in re.split(r"(\033\[[0-9;]*m)", line):
            code = re.fullmatch(r"\033\[([0-9;]*)m", part)
            if code:
                colour = COLOURS.get(code.group(1), TEXT)
                continue
            pen.text((20 + col * cell_w, 20 + row * cell_h), part, font=font, fill=colour)
            col += len(part)
    image.save(path)


def start_up_screen() -> list[str]:
    """The volatile lab's start-up screen (snaplab/delivery/usb/live/snap-splash), mid-way."""
    from snaplab.delivery.usb import volatile

    status = "Waiting for a network address from your router"
    return volatile.splash(COLS).splitlines() + ["", f"      \033[1;36m>\033[0m {status}"]


def main() -> None:
    if "--host" not in sys.argv:
        from_tests()
    for picture, lines in {**host_screens(), **host_screens("volatile")}.items():
        draw(lines, HERE / picture)
    draw(start_up_screen(), HERE / "17-volatile-start-up.png")
    print(f"pictures in {HERE}")


if __name__ == "__main__":
    main()
