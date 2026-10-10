"""Download and verify pinned third-party artifacts (Design 0001, section 5.1)."""

from __future__ import annotations

import hashlib
import shutil
import tomllib
import urllib.request
from pathlib import Path
from typing import Any

CHUNK = 1 << 20


class FetchError(Exception):
    pass


def load_lock(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise FetchError(f"cannot read {path}: {e}") from e


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, expected_sha256: str, cache: Path, log=print) -> Path:
    """Return a verified local copy of `url`, downloading it only if the cache lacks a good copy."""
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / url.rsplit("/", 1)[-1]
    if target.exists():
        if sha256(target) == expected_sha256:
            log(f"using cached {target.name}")
            return target
        log(f"cached {target.name} does not match its pinned checksum; downloading again")
        target.unlink()

    partial = target.with_name(target.name + ".part")
    log(f"downloading {url}")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, partial.open("wb") as out:
            shutil.copyfileobj(resp, out, CHUNK)
    except OSError as e:
        partial.unlink(missing_ok=True)
        raise FetchError(f"download of {url} failed: {e}") from e

    actual = sha256(partial)
    if actual != expected_sha256:
        partial.unlink()
        raise FetchError(f"{target.name} has SHA-256 {actual}, but checksums.lock pins {expected_sha256}; refusing it")
    partial.rename(target)
    return target
