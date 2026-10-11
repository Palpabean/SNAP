import hashlib

import pytest

from snaplab.delivery.usb import fetch

DATA = b"pretend this is an ISO\n" * 1000
GOOD = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "mirror" / "proxmox-ve_9.2-1.iso"
    src.parent.mkdir()
    src.write_bytes(DATA)
    return src.as_uri()


def test_download_and_verify(tmp_path, source):
    got = fetch.fetch(source, GOOD, tmp_path / "cache", log=lambda _: None)
    assert got.read_bytes() == DATA
    assert got.name == "proxmox-ve_9.2-1.iso"
    assert not list((tmp_path / "cache").glob("*.part"))


def test_cached_copy_is_reused(tmp_path, source):
    cache = tmp_path / "cache"
    fetch.fetch(source, GOOD, cache, log=lambda _: None)
    (tmp_path / "mirror" / "proxmox-ve_9.2-1.iso").unlink()
    messages = []
    fetch.fetch(source, GOOD, cache, log=messages.append)
    assert messages == ["using cached proxmox-ve_9.2-1.iso"]


def test_tampered_cache_is_replaced(tmp_path, source):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "proxmox-ve_9.2-1.iso").write_bytes(b"tampered")
    assert fetch.fetch(source, GOOD, cache, log=lambda _: None).read_bytes() == DATA


def test_wrong_checksum_is_refused(tmp_path, source):
    with pytest.raises(fetch.FetchError, match="refusing it"):
        fetch.fetch(source, "0" * 64, tmp_path / "cache", log=lambda _: None)
    assert not list((tmp_path / "cache").iterdir())


def test_failed_download(tmp_path):
    with pytest.raises(fetch.FetchError, match="failed"):
        fetch.fetch((tmp_path / "missing.iso").as_uri(), GOOD, tmp_path / "cache", log=lambda _: None)


def test_repo_lock_pins_proxmox():
    from pathlib import Path

    lock = fetch.load_lock(Path(__file__).parent.parent / "checksums.lock")
    pve = lock["proxmox-ve"]
    assert pve["url"].endswith(f"proxmox-ve_{pve['version']}.iso")
    assert len(pve["sha256"]) == 64
    assert lock["proxmox-auto-install-assistant"]["version"].startswith(pve["version"].split("-")[0])
    ubuntu = lock["ubuntu-cloud-image"]
    assert ubuntu["url"].startswith("https://cloud-images.ubuntu.com/") and len(ubuntu["sha256"]) == 64
