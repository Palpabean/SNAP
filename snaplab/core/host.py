"""The host the engine works on: commands and files (Design 0001, section 5.4).

Stages talk to the machine only through this class, so tests can swap in a
fake host. Standard library only: it runs on the Proxmox host, which has
python3 but none of the builder's packages.
"""

from __future__ import annotations

import os
import ssl
import subprocess
import urllib.request
import zipfile
from pathlib import Path


class CommandError(Exception):
    def __init__(self, cmd: list[str], code: int, output: str):
        self.cmd, self.code, self.output = cmd, code, output
        tail = " / ".join(output.strip().splitlines()[-3:])
        super().__init__(f"{' '.join(cmd)} failed ({code}): {tail}")


class Host:
    def __init__(self, root: str | Path = "/"):
        # Every file path is taken relative to root, so tests can use a directory.
        self.root = Path(root)

    def path(self, path: str | Path) -> Path:
        return self.root / str(path).lstrip("/")

    def run(self, *cmd: str, input: str | None = None) -> str:
        """Run a command and return its output; raise CommandError if it fails."""
        proc = subprocess.run(list(cmd), input=input, capture_output=True, text=True)
        if proc.returncode != 0:
            raise CommandError(list(cmd), proc.returncode, proc.stdout + proc.stderr)
        return proc.stdout

    def ok(self, *cmd: str) -> bool:
        """True if the command succeeds. For checks; output is discarded."""
        return subprocess.run(list(cmd), capture_output=True).returncode == 0

    def exists(self, path: str | Path) -> bool:
        return self.path(path).exists()

    def read(self, path: str | Path) -> str | None:
        try:
            return self.path(path).read_text()
        except FileNotFoundError:
            return None

    def write(self, path: str | Path, text: str, mode: int = 0o644) -> None:
        """Write a file atomically."""
        target = self.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.snap-tmp")
        tmp.write_text(text)
        tmp.chmod(mode)
        os.replace(tmp, target)

    def append(self, path: str | Path, text: str) -> None:
        target = self.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a") as f:
            f.write(text)

    def mkdir(self, path: str | Path, mode: int = 0o755) -> None:
        target = self.path(path)
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(mode)

    def unzip(
        self, archive: str | Path, dest: str | Path, members: list[str] | None = None, mode: int = 0o755
    ) -> list[str]:
        """Extract a zip archive (or only `members`) into dest; return the extracted names."""
        target = self.path(dest)
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(self.path(archive)) as z:
            names = [n for n in z.namelist() if not n.endswith("/") and (members is None or n in members)]
            missing = set(members or []) - set(names)
            if missing:
                raise RuntimeError(f"{archive} has no {', '.join(sorted(missing))}")
            z.extractall(target, members=names)
        for name in names:
            (target / name).chmod(mode)
        return names

    def http_ok(self, url: str, headers: dict[str, str]) -> bool:
        """True if a GET succeeds. Certificates are not checked: only for this host's own API."""
        context = ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), context=context, timeout=15):
                return True
        except OSError:
            return False

    def glob(self, pattern: str) -> list[str]:
        return sorted("/" + str(p.relative_to(self.root)) for p in self.root.glob(pattern.lstrip("/")))
