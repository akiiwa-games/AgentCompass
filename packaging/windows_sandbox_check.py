"""Run guest_check.py inside a real Windows Sandbox: a clean, offline Windows with its own user, registry and PATH.

Host side only orchestrates. The guest gets exactly two mapped folders and no network, clipboard, audio, video, printer or GPU:

    C:\\in   (read-only)   staged portable Python, the wheel built from this working tree, fixture provider CLIs, node, hc, guest_check.py
    C:\\out  (writable)    results.json, logs, desktop.png

Nothing of the real home, credentials, worktree or services is mapped. Safety rules this script keeps:
  * it refuses to start when any Windows Sandbox is already running (it never touches a sandbox it did not start);
  * the sandbox window is minimised without activation right after start (no focus stealing);
  * it always runs `wsb stop` for the sandbox it started, then confirms `wsb list` is empty again;
  * every run writes a new directory under .route-briefs/windows-sandbox-report/ and never clears old ones.

    python -X utf8 packaging/windows_sandbox_check.py [--keep-stage] [--only STEP ...]
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sandbox_check as sc  # noqa: E402
import zero_dispatch_check as zd  # noqa: E402

ROOT = sc.ROOT
HC = ROOT / ".route-briefs" / "upstream" / "herdr-portable"
PYTHON_HOME = Path(sys.executable).resolve().parent
NODE = shutil.which("node")


def wsb(*args, timeout=120):
    done = subprocess.run(["wsb", *args], capture_output=True, timeout=timeout)
    text = (done.stdout + done.stderr).decode("utf-8", "replace")
    return done.returncode, text


def running_sandboxes() -> list[str]:
    code, text = wsb("list", "--raw")
    return re.findall(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}", text)


def minimise_sandbox_windows() -> int:
    """SW_SHOWMINNOACTIVE on the Windows Sandbox client windows: out of sight, without taking focus."""
    script = r'''
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class W { [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr h, int c); }'
$n = 0
Get-Process | Where-Object { $_.ProcessName -match 'WindowsSandbox' -and $_.MainWindowHandle -ne 0 } | ForEach-Object { [W]::ShowWindowAsync($_.MainWindowHandle, 7) | Out-Null; $n++ }
$n
'''
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True)
    return int(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() else 0


TOOLS = Path(tempfile.gettempdir()) / "agentcompass-tools"          # task-temp tools: the Inno Setup installer is downloaded here, never installed on the host
GUI_STEPS = ("gui_tray", "installer")


def build_frozen(repo: Path, base: Path) -> Path:
    print("staging: frozen tray (PyInstaller, isolated source copy) ...", flush=True)
    dist = base / "frozen-dist"
    done = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", str(dist), "--workpath", str(base / "frozen-work"),
                           str(repo / "packaging" / "agentcompass-tray.spec")], cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1500)
    if done.returncode != 0 or not (dist / "AgentCompass" / "AgentCompass.exe").is_file():
        raise SystemExit("PyInstaller failed: " + (done.stderr or done.stdout)[-1500:])
    return dist / "AgentCompass"


def stage(base: Path, repo_root: Path, gui: bool = False, inno: bool = False) -> Path:
    stage_in = base / "in"
    stage_in.mkdir()
    print("staging: python ...", flush=True)
    subprocess.run(["robocopy", str(PYTHON_HOME), str(stage_in / "python"), "/E", "/MT:8", "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
                    "/XD", "site-packages", "test", "tests", "idlelib", "turtledemo", "Doc", "Tools", "__pycache__", "include"], capture_output=True)
    (stage_in / "python" / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
    print("staging: wheel (built from the working tree) ...", flush=True)
    repo = sc.build_fixture_repo(base)
    (stage_in / "build-source-manifest.json").write_text(json.dumps({p.relative_to(repo).as_posix(): sha256_of(p) for p in sorted(repo.rglob("*"))
                                                                  if p.is_file() and ".git" not in p.relative_to(repo).parts}, indent=0), encoding="utf-8")
    wheel = sc.build_wheel(repo, base / "dist")
    (stage_in / "wheel").mkdir()
    shutil.copy2(wheel, stage_in / "wheel" / wheel.name)
    print("staging: fixture provider CLIs ...", flush=True)
    bin_dir = zd.build_fake_clis(base)
    (stage_in / "fixtures").mkdir()
    shutil.copytree(bin_dir, stage_in / "fixtures" / "bin")
    (stage_in / "node").mkdir()
    shutil.copy2(NODE, stage_in / "node" / "node.exe")
    shutil.copytree(HC, stage_in / "hc")
    if gui:
        shutil.copytree(build_frozen(repo, base), stage_in / "frozen" / "AgentCompass")
    if inno:
        installer = next(TOOLS.glob("innosetup-*.exe"), None)
        if not installer:
            raise SystemExit(f"the Inno Setup installer is missing: download innosetup-6.x.exe into {TOOLS} (it is only run inside the sandbox)")
        (stage_in / "inno" / "packaging").mkdir(parents=True)
        shutil.copy2(installer, stage_in / "inno" / installer.name)
        shutil.copy2(repo / "packaging" / "agentcompass.iss", stage_in / "inno" / "packaging" / "agentcompass.iss")
        version = re.search(r"__version__\s*=\s*['" + chr(34) + r"]([^'" + chr(34) + r"]+)", (repo / "agentcompass" / "__init__.py").read_text(encoding="utf-8")).group(1)
        (stage_in / "inno" / "version.txt").write_text(version, encoding="utf-8")
    (stage_in / "guest").mkdir()
    shutil.copy2(HERE / "guest_check.py", stage_in / "guest" / "guest_check.py")
    return stage_in


def config(stage_in: Path, out: Path) -> str:
    return (f"<Configuration><VGpu>Disable</VGpu><Networking>Disable</Networking><AudioInput>Disable</AudioInput><VideoInput>Disable</VideoInput>"
            f"<PrinterRedirection>Disable</PrinterRedirection><ClipboardRedirection>Disable</ClipboardRedirection><ProtectedClient>Enable</ProtectedClient>"
            f"<MemoryInMB>4096</MemoryInMB><MappedFolders>"
            f"<MappedFolder><HostFolder>{stage_in}</HostFolder><SandboxFolder>C:\\in</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>"
            f"<MappedFolder><HostFolder>{out}</HostFolder><SandboxFolder>C:\\out</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>"
            f"</MappedFolders></Configuration>")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", type=Path, default=ROOT / ".route-briefs" / "windows-sandbox-report")
    parser.add_argument("--keep-stage", action="store_true")
    parser.add_argument("--only", nargs="*", help="guest steps to run (isolation, stage and install always run)")
    parser.add_argument("--connect", action="store_true", help="open an interactive session with `wsb connect` (hidden helper) so ExistingLogin and GUI steps can run")
    parser.add_argument("--timeout", type=int, default=1500)
    parser.add_argument("--artifacts", type=Path, help="keep the wheel, frozen tray folder and (installer step) installer that were tested, with sha256 and a source manifest, in this new directory")
    args = parser.parse_args(argv)
    if not shutil.which("wsb"):
        print("wsb.exe is not available: Windows Sandbox is not enabled on this machine", file=sys.stderr)
        return 2
    import py_compile
    py_compile.compile(str(HERE / "guest_check.py"), doraise=True)       # a guest script that cannot even compile must not cost a sandbox boot
    existing = running_sandboxes()
    if existing:
        print(f"refusing to start: a Windows Sandbox is already running ({existing}); it is not ours to touch", file=sys.stderr)
        return 2
    started = time.time()
    run_dir = args.report / time.strftime("run-%Y%m%d-%H%M%S")
    out = run_dir / "guest-out"
    out.mkdir(parents=True)
    base = Path(tempfile.mkdtemp(prefix="agentcompass-wsb-"))
    notes = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "stage": str(base), "events": []}

    def note(text):
        notes["events"].append(f"{time.time() - started:6.1f}s {text}")
        print(note_prefix + text, flush=True)
    note_prefix = "  "
    sandbox_id = None
    failures = 1
    stage_in = None
    try:
        wants_gui = not args.only or any(step in GUI_STEPS for step in args.only)
        stage_in = stage(base, ROOT, gui=wants_gui, inno=not args.only or "installer" in args.only)
        code, text = wsb("start", "--raw", "--config", config(stage_in, out.resolve()), timeout=180)
        note(f"wsb start rc={code}: {text.strip()[:200]}")
        found = re.findall(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}", text)
        sandbox_id = found[0] if found else (running_sandboxes() or [None])[0]
        if not sandbox_id:
            notes["fatal"] = "the sandbox did not start"
            return 2
        if args.connect:
            # An interactive logon session (needed for ExistingLogin and any GUI) exists only while a client is connected.
            # The connect helper is started hidden; the window it may still create is minimised without activation.
            helper = subprocess.Popen(["wsb", "connect", "--id", sandbox_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            notes["connect_pid"] = helper.pid
            note(f"wsb connect helper started (pid {helper.pid}, no console window)")
        for _ in range(10 if not args.connect else 30):
            time.sleep(1)
            if minimise_sandbox_windows():
                note("sandbox window minimised without activation")
                break
        mode, ready = None, False
        deadline = time.time() + 150            # the sandbox signs its user in a little after it boots; System is the fallback
        while time.time() < deadline and not ready:
            code, text = wsb("exec", "--id", sandbox_id, "-r", "ExistingLogin", "-c", r"cmd /c echo ready> C:\out\ready.txt", timeout=60)
            notes.setdefault("exec_attempts", []).append(f"ExistingLogin rc={code} {text.strip()[:120]}")
            if code == 0 and (out / "ready.txt").exists():
                mode, ready = "ExistingLogin", True
            else:
                time.sleep(5)
        if not ready:
            code, text = wsb("exec", "--id", sandbox_id, "-r", "System", "-c", r"cmd /c echo ready> C:\out\ready.txt", timeout=60)
            notes["exec_attempts"].append(f"System rc={code} {text.strip()[:120]}")
            if code == 0 and (out / "ready.txt").exists():
                mode, ready = "System", True
        note(f"guest ready as {mode}" if ready else "guest never answered an exec")
        notes["run_as"] = mode
        if not ready:
            notes["fatal"] = "no exec session"
            return 2
        command = r'"C:\in\python\python.exe" -X utf8 "C:\in\guest\guest_check.py"' + (" " + " ".join(args.only) if args.only else "")
        code, text = wsb("exec", "--id", sandbox_id, "-r", mode, "-c", command, timeout=args.timeout)
        note(f"guest_check.py exec rc={code} {text.strip()[:200]}")
        wait_until = time.time() + args.timeout
        while time.time() < wait_until and not (out / "DONE").exists():
            time.sleep(3)
        note("guest finished" if (out / "DONE").exists() else "guest did not finish in time")
        data = json.loads((out / "results.json").read_text(encoding="utf-8")) if (out / "results.json").is_file() else {"results": [], "failures": 1}
        failures = data["failures"] + (0 if (out / "DONE").exists() else 1)
        notes["guest_results"] = data
        return failures
    finally:
        if notes.get("connect_pid"):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(notes["connect_pid"])], capture_output=True)
        if sandbox_id:
            code, text = wsb("stop", "--id", sandbox_id, timeout=120)
            note(f"wsb stop rc={code}")
        time.sleep(3)
        left = running_sandboxes()
        notes["left_running"] = left
        note("no sandbox left running" if not left else f"STILL RUNNING: {left}")
        if left:
            for item in left:
                wsb("stop", "--id", item)
        if args.artifacts and stage_in:
            keep_artifacts(args.artifacts, stage_in, out, failures)
        if not args.keep_stage:
            shutil.rmtree(base, ignore_errors=True)
        write_report(run_dir, notes, started)
        print(f"windows sandbox check: {failures} failed assertion(s); report {run_dir / 'report.md'}")


def sha256_of(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def keep_artifacts(target: Path, stage_in: Path, guest_out: Path, failures: int):
    """The exact files this run installed and ran. Built from a throw-away copy of the working tree (tracked + untracked, not ignored); the provider
    fixture executables are staged separately under fixtures/ and are never part of any of these."""
    target.mkdir(parents=True, exist_ok=False)
    if (stage_in / "wheel").is_dir():
        shutil.copytree(stage_in / "wheel", target / "wheel")
    if (stage_in / "frozen").is_dir():
        shutil.copytree(stage_in / "frozen" / "AgentCompass", target / "frozen" / "AgentCompass")
    if (guest_out / "release").is_dir():
        shutil.copytree(guest_out / "release", target / "installer")
    fixtures = [str(f) for f in target.rglob("*") if f.name.lower() in ("codex.exe", "claude.exe", "zero_fake_cli.py")]
    if fixtures:
        raise SystemExit(f"provider fixture executables are inside the candidate artifacts: {fixtures}")
    if (stage_in / "build-source-manifest.json").is_file():
        shutil.copy2(stage_in / "build-source-manifest.json", target / "build-source-manifest.json")
    listing = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard", "-z"], capture_output=True, check=True).stdout
    sources = {raw.decode("utf-8"): sha256_of(ROOT / raw.decode("utf-8")) for raw in filter(None, listing.split(bytes(1))) if (ROOT / raw.decode("utf-8")).is_file()
               and not raw.decode("utf-8").startswith(("usage-float/", "dist/", "scratch/", "pip/", "tools/"))}
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (target / "live-tree-at-end-of-run-manifest.json").write_text(json.dumps({"git_head": head, "files": sources}, indent=0), encoding="utf-8")
    hashes = {str(p.relative_to(target)).replace(chr(92), "/"): sha256_of(p) for p in sorted(target.rglob("*")) if p.is_file() and p.name != "SHA256SUMS.txt"}
    (target / "SHA256SUMS.txt").write_text("".join(f"{h}  {n}" + chr(10) for n, h in hashes.items()), encoding="utf-8")
    (target / "README.txt").write_text("Candidate artifacts of a Windows Sandbox run (not signed). wheel: installed in the guest. frozen/AgentCompass: the PyInstaller one-folder tray run in the guest. "
                                       "installer: built inside the guest by Inno Setup from that folder (installer step only). build-source-manifest.json: sha256 of every file of the throw-away build tree the wheel and frozen folder were built from; "
                                       "live-tree-at-end-of-run-manifest.json: the live working tree when the run ended (not the build snapshot). Guest assertion failures in the run that produced them: " + str(failures) + chr(10), encoding="utf-8")
    print(f"artifacts kept in {target}")


def write_report(run_dir: Path, notes: dict, started: float):
    data = notes.get("guest_results") or {"results": []}
    lines = ["# Windows Sandbox run", "", f"- started {notes['started']}, {time.time() - started:.0f}s, guest user context: {notes.get('run_as')}",
             f"- fatal: {notes.get('fatal')}" if notes.get("fatal") else "- no fatal error", f"- sandboxes left running afterwards: {notes.get('left_running')}", "", "## Events", ""]
    lines += [f"- {e}" for e in notes["events"]]
    lines += ["", "## Guest assertions", ""]
    last = None
    for r in data["results"]:
        if r["section"] != last:
            lines.append(f"\n### {r['section']} {r['title']}")
            last = r["section"]
        lines.append(f"- {'ok  ' if r['ok'] else 'FAIL'} {r['name']}" + ("" if r["ok"] else f" — {r['detail']}"))
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (run_dir / "report.json").write_text(json.dumps(notes, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
