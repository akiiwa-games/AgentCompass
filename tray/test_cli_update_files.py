"""Windows image replacement using only temporary installs and owned processes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cli_update_files as files
import cli_updates as cli
import owned_process


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def manifest(path, name, **extra):
    write(path / "package.json", json.dumps(dict(name=name, version="1.0.0", **extra)))


def test_dependency_closure_covers_nested_and_hoisted_optional_images_only(tmp_path):
    package = tmp_path / "node_modules/@openai/codex"
    optional = tmp_path / "node_modules/@openai/codex-win32-x64"
    nested = optional / "node_modules/native-helper"
    unrelated = tmp_path / "node_modules/unrelated"
    manifest(package, "@openai/codex", optionalDependencies={"@openai/codex-win32-x64": "1"})
    manifest(optional, "@openai/codex-win32-x64", dependencies={"native-helper": "1"})
    manifest(nested, "native-helper", dependencies={"@openai/codex": "1"})  # a cycle
    manifest(unrelated, "unrelated")
    assert set(files.npm_roots(package, tmp_path)) == {package, optional, nested}


def locked_probe(monkeypatch, path):
    original = Path.open
    def open_file(self, mode="r", *args, **kwargs):
        if self == path and mode == "r+b":
            raise PermissionError("fixture image lock")
        return original(self, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", open_file)


def test_unlocked_file_is_untouched(tmp_path):
    path = tmp_path / "tool.exe"
    path.write_bytes(b"old")
    remember = mock.Mock()
    assert files.retain_locked(path, tmp_path / files.RETIRED_DIRECTORY, remember) is None
    remember.assert_not_called()
    assert path.read_bytes() == b"old"


@pytest.mark.parametrize("operation", ["copy", "record"])
def test_retention_failure_restores_original_path(tmp_path, monkeypatch, operation):
    path = tmp_path / "tool.exe"
    path.write_bytes(b"old")
    locked_probe(monkeypatch, path)
    remember = mock.Mock()
    if operation == "copy":
        def copy(*args):
            path.write_bytes(b"partial")
            raise OSError("fixture copy failed")
        monkeypatch.setattr(files.shutil, "copy2", copy)
    else:
        remember.side_effect = OSError("fixture state write failed")
    with pytest.raises(OSError):
        files.retain_locked(path, tmp_path / files.RETIRED_DIRECTORY, remember)
    assert path.read_bytes() == b"old"
    assert not list((tmp_path / files.RETIRED_DIRECTORY).iterdir())


def test_cleanup_refuses_unowned_paths(tmp_path):
    path = tmp_path / "tool.exe"
    path.write_bytes(b"keep")
    with pytest.raises(ValueError):
        files.remove_retired(path)
    assert path.read_bytes() == b"keep"


@pytest.fixture(scope="module")
def image_versions(tmp_path_factory):
    if os.name != "nt":
        pytest.skip("requires Windows image locks")
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        pytest.skip("requires the Windows .NET Framework C# compiler")
    directory = tmp_path_factory.mktemp("cli-images")
    images = []
    for version in ("old", "new"):
        source, image = directory / f"{version}.cs", directory / f"{version}.exe"
        write(source, '''using System;
using System.IO;
class Program {
    static void Main(string[] args) {
        if (args[0] == "--version") { Console.WriteLine("VERSION"); return; }
        File.WriteAllText(args[0], "VERSION");
        string reply;
        while ((reply = Console.ReadLine()) != null) File.WriteAllText(reply, "VERSION");
    }
}
'''.replace("VERSION", version))
        subprocess.run([str(compiler), "/nologo", f"/out:{image}", str(source)],
                       check=True, capture_output=True, timeout=30,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        images.append(image)
    return images


def wait_file(path, process):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        assert process.poll() is None
        if path.exists() and path.read_text():
            return path.read_text()
        time.sleep(.02)
    raise AssertionError(f"owned image did not answer: {path}")


@pytest.mark.slow
@pytest.mark.parametrize("layout", ["nested", "hoisted"])
def test_running_image_keeps_old_code_and_new_launch_uses_replacement(tmp_path, monkeypatch, image_versions, layout):
    # Rule: replace a running CLI without terminating it, then clean only after exit.
    prefix = tmp_path / "npm prefix"
    package = prefix / "node_modules/@openai/codex"
    dependency = (package if layout == "nested" else prefix / "node_modules/@openai/codex-win32-x64")
    extra = {} if layout == "nested" else {"optionalDependencies": {"@openai/codex-win32-x64": "1"}}
    manifest(package, "@openai/codex", **extra)
    if layout == "hoisted":
        manifest(dependency, "@openai/codex-win32-x64")
    image = dependency / "vendor/bin/codex.exe"
    image.parent.mkdir(parents=True)
    shutil.copy2(image_versions[0], image)
    shim = prefix / "codex.cmd"
    write(shim, '@ECHO off\n"%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\n')
    install = cli.Install(cli.BY_ID["codex"], shim, "npm", package, prefix, ["fixture-npm"], configured=str(shim))
    target = cli.Target("codex", "Codex CLI", install, ["codex"])
    monkeypatch.setattr(cli, "find", lambda *args: target)
    monkeypatch.setattr(cli, "_busy", {})
    calls = []
    def run(argv, timeout, install):
        calls.append(argv)
        if "view" in argv:
            return owned_process.Result(0, "2.0.0", "")
        # This overwrite would fail with WinError 32/5 if preflight had not retained the image.
        shutil.copy2(image_versions[1], image)
        write(package / "package.json", json.dumps(dict(name="@openai/codex", version="2.0.0", **extra)))
        return owned_process.Result(0, "installed", "")
    monkeypatch.setattr(cli, "run_tool", run)
    ready, reply = tmp_path / "ready", tmp_path / "reply"
    process = subprocess.Popen([str(image), str(ready)], stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert wait_file(ready, process) == "old"
        with pytest.raises(PermissionError):
            with image.open("r+b"):
                pass
        outcome = {}
        cli.update("codex", outcome=outcome)
        assert outcome["status"] == "updated"
        assert len([argv for argv in calls if "install" in argv]) == 1
        assert process.poll() is None
        new = subprocess.run([str(image), "--version"], check=True, capture_output=True,
                             text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        assert new.stdout.strip() == "new"
        process.stdin.write(str(reply) + "\n")
        process.stdin.flush()
        assert wait_file(reply, process) == "old"
        retained = [Path(raw) for raw in cli._load_state()["retired_files"]]
        assert len(retained) == 1 and retained[0].is_file()
        cli.cleanup_retired()
        assert retained[0].exists()  # still in use; cleanup must leave it alone
        assert process.poll() is None
    finally:
        process.stdin.close()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(5)
    cli.maybe_trigger_auto_update(uptime=0)  # cleanup works even with automatic updates disabled
    assert not retained[0].exists()
    assert cli._load_state()["retired_files"] == []


@pytest.mark.skipif(os.name != "nt", reason="Windows preflight")
def test_failed_rename_is_an_update_failure_not_a_deferred_session(tmp_path, monkeypatch):
    package = tmp_path / "node_modules/@openai/codex"
    manifest(package, "@openai/codex")
    image = package / "codex.exe"
    image.write_bytes(b"old")
    locked_probe(monkeypatch, image)
    monkeypatch.setattr(Path, "rename", mock.Mock(side_effect=PermissionError("fixture rename denied")))
    target = cli.Target("codex", "Codex CLI", cli.Install(cli.BY_ID["codex"], image, "npm", package, tmp_path, ["fake"]), [])
    monkeypatch.setattr(cli, "find", lambda *args: target)
    monkeypatch.setattr(cli, "resolve_command", lambda *args, **kwargs: image)
    monkeypatch.setattr(cli, "_busy", {})
    tool = mock.Mock(return_value=owned_process.Result(0, "2.0.0", ""))
    monkeypatch.setattr(cli, "run_tool", tool)
    outcome = {}
    cli.update("codex", outcome=outcome)
    assert outcome["status"] == "failed"
    assert "fixture rename denied" in outcome["message"]
    assert len(tool.call_args_list) == 1  # only the version query; no destructive installer
    assert image.read_bytes() == b"old"
    assert cli._busy == {}
