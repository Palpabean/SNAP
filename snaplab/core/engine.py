"""The SNAP engine: runs stages as recorded, idempotent steps (Design 0001, sections 5.4 and 9).

Each step observes the machine first (`done`) and acts only when needed
(`apply`), then observes again to confirm the change took effect. Results go
to a state file, so `snap resume` continues at the first unfinished step after
any restart, and `snap status` shows where setup is. A finished stage writes
/var/lib/snap/progress/<stage>.done, which the USB boot menu reads.

Standard library only (runs on the Proxmox host).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from snaplab.core.host import Host

STATE_DIR = "/var/lib/snap"
STATE_FILE = f"{STATE_DIR}/state.json"
PROGRESS_DIR = f"{STATE_DIR}/progress"
LOG_FILE = "/var/log/snap/engine.log"


class StageFailed(Exception):
    pass


@dataclass
class Context:
    host: Host
    cfg: dict[str, Any]
    node: str
    say: Callable[[str], None] = print
    payload: str = STATE_DIR


@dataclass
class Step:
    name: str
    title: str
    done: Callable[[Context], bool]
    apply: Callable[[Context], None]


@dataclass
class Stage:
    name: str
    title: str
    steps: list[Step]
    preflight: Callable[[Context], list[str]] = field(default=lambda ctx: [])


class Engine:
    def __init__(self, stages: list[Stage], ctx: Context, clock: Callable[[], float] = time.time):
        self.stages, self.ctx, self.clock = stages, ctx, clock
        self.host = ctx.host

    # --- state ---------------------------------------------------------

    def load(self) -> dict[str, Any]:
        text = self.host.read(STATE_FILE)
        return json.loads(text) if text else {"stages": {}}

    def save(self, state: dict[str, Any]) -> None:
        self.host.write(STATE_FILE, json.dumps(state, indent=2, sort_keys=True) + "\n", mode=0o600)

    def is_done(self, stage: Stage) -> bool:
        return self.host.exists(f"{PROGRESS_DIR}/{stage.name}.done")

    def log(self, message: str) -> None:
        line = time.strftime("%Y-%m-%d %H:%M:%S ", time.localtime(self.clock())) + message + "\n"
        self.host.append(LOG_FILE, line)

    def say(self, message: str) -> None:
        self.log(message)
        self.ctx.say(f"SNAP: {message}")

    # --- running ---------------------------------------------------------

    def resume(self) -> bool:
        """Run every unfinished stage in order. True when all of them are done."""
        for stage in self.stages:
            if self.is_done(stage):
                continue
            try:
                self.run_stage(stage)
            except StageFailed as e:
                self.say(f"{stage.title} failed: {e}")
                self.say("Fix the cause, then run 'snap resume' (or restart) to continue from this step.")
                return False
        return True

    def run_stage(self, stage: Stage) -> None:
        state = self.load()
        record = state["stages"].setdefault(stage.name, {"steps": {}})
        record.update(status="running", started=record.get("started") or self.clock())
        self.save(state)
        self.say(f"{stage.title} started")

        problems = stage.preflight(self.ctx)
        if problems:
            self._fail(state, record, None, "; ".join(problems))

        for i, step in enumerate(stage.steps, 1):
            entry = record["steps"].setdefault(step.name, {})
            prefix = f"[{i}/{len(stage.steps)}] {step.title}"
            try:
                if step.done(self.ctx):
                    entry.update(status="done", at=self.clock())
                    self.say(f"{prefix}: already done")
                else:
                    entry.update(status="running", at=self.clock())
                    self.save(state)
                    self.say(f"{prefix} ...")
                    step.apply(self.ctx)
                    if not step.done(self.ctx):
                        raise StageFailed("the change did not take effect")
                    entry.update(status="done", at=self.clock())
                    entry.pop("error", None)
                    self.say(f"{prefix}: done")
            except Exception as e:  # noqa: BLE001 - any failure is recorded and reported
                self._fail(state, record, entry, f"{step.title}: {e}")
            self.save(state)

        # Verify: every step must still hold before the stage counts as done.
        broken = [s.title for s in stage.steps if not s.done(self.ctx)]
        if broken:
            self._fail(state, record, None, "final check failed for: " + ", ".join(broken))

        record.update(status="done", finished=self.clock())
        self.save(state)
        self.host.write(f"{PROGRESS_DIR}/{stage.name}.done", f"{self.clock():.0f}\n")
        self.say(f"{stage.title} done")

    def _fail(self, state, record, entry, message: str):
        record.update(status="failed", error=message)
        if entry is not None:
            entry.update(status="failed", error=message, at=self.clock())
        self.save(state)
        raise StageFailed(message)

    # --- reporting -------------------------------------------------------

    def status(self) -> list[str]:
        state = self.load()
        lines = []
        for stage in self.stages:
            record = state["stages"].get(stage.name, {})
            status = "done" if self.is_done(stage) else record.get("status", "pending")
            lines.append(f"{stage.title}: {status}")
            for step in stage.steps:
                entry = record.get("steps", {}).get(step.name, {})
                line = f"  - {step.title}: {entry.get('status', 'pending')}"
                if entry.get("error"):
                    line += f" ({entry['error']})"
                lines.append(line)
        return lines
