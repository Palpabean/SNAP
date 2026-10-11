import pytest

from snaplab.core.host import CommandError, Host


def test_missing_command_is_not_ok(tmp_path):
    host = Host(tmp_path)
    assert host.ok(str(tmp_path / "no-such-tool"), "version") is False


def test_missing_command_raises_command_error(tmp_path):
    host = Host(tmp_path)
    with pytest.raises(CommandError, match="command not found"):
        host.run(str(tmp_path / "no-such-tool"))


def test_failing_command_output_in_error(tmp_path):
    with pytest.raises(CommandError, match="boom"):
        Host(tmp_path).run("sh", "-c", "echo boom >&2; exit 3")


def test_unzip_selected_member(tmp_path):
    import zipfile

    host = Host(tmp_path)
    with zipfile.ZipFile(tmp_path / "a.zip", "w") as z:
        z.writestr("tofu", "bin")
        z.writestr("LICENSE", "text")
    assert host.unzip("/a.zip", "/out", members=["tofu"]) == ["tofu"]
    assert (tmp_path / "out/tofu").exists() and not (tmp_path / "out/LICENSE").exists()
    with pytest.raises(RuntimeError, match="has no missing"):
        host.unzip("/a.zip", "/out", members=["missing"])
