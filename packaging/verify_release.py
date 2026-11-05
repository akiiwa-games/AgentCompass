"""Reusable pstack release verification runner for AgentCompass and usage-float.

    python -X utf8 packaging/verify_release.py [--wheel X.whl] [--with-windows-sandbox] [--report DIR]

Runs the full pre-release verification suite:
  1. Foreign-machine packaged wheel & cloned usage-float behavioral verification (F01..F13)
  2. Zero-credential 13-surface dispatch & scorer verification (zero_dispatch_check.py)
  3. Unit/regression suites across setup/, skills/agent-compass/scripts/, tray/, and usage-float/
  4. Optional real Windows Sandbox installer + tray verification (W01, --with-windows-sandbox)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
USAGE_FLOAT_DIR = ROOT / "usage-float"


def _find_tray_python() -> str:
    candidates = [
        Path(sys.executable),
        Path.home() / "AppData/Local/Programs/Python/Python313/python.exe",
        Path.home() / "AppData/Local/Programs/Python/Python312/python.exe",
    ]
    for cand in candidates:
        if not cand.is_file():
            continue
        res = subprocess.run([str(cand), "-c", "import pystray, PIL, psutil, pytest"], capture_output=True)
        if res.returncode == 0:
            return str(cand)
    return sys.executable


def _run_step(name: str, argv: list[str], cwd: Path = ROOT) -> dict:
    started = time.time()
    print(f"==> [{name}] {' '.join(str(a) for a in argv)}", flush=True)
    proc = subprocess.run(argv, cwd=str(cwd), text=True, encoding="utf-8", errors="replace", capture_output=True)
    elapsed = round(time.time() - started, 1)
    status = "PASS" if proc.returncode == 0 else "FAIL"
    print(f"    [{status}] {name} ({elapsed}s, rc={proc.returncode})", flush=True)
    if proc.returncode != 0:
        tail = (proc.stdout + "\n" + proc.stderr).strip()[-1500:]
        print(f"    --- failure tail ---\n{tail}\n    --------------------", flush=True)
    return {
        "name": name,
        "argv": [str(a) for a in argv],
        "cwd": str(cwd),
        "returncode": proc.returncode,
        "status": status,
        "elapsed_seconds": elapsed,
        "stdout_tail": (proc.stdout or "")[-2000:],
        "stderr_tail": (proc.stderr or "")[-2000:],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wheel", type=Path, help="Existing wheel to verify.")
    parser.add_argument("--with-windows-sandbox", action="store_true", help="Also run windows_sandbox_check.py --connect (W01).")
    parser.add_argument("--report", type=Path, default=ROOT / ".route-briefs" / "verify-release-report")
    args = parser.parse_args(argv)

    py = _find_tray_python()
    started = time.time()
    steps: list[dict] = []

    foreign_cmd = [py, "-X", "utf8", str(HERE / "foreign_machine_check.py")]
    if args.wheel:
        foreign_cmd.extend(["--wheel", str(args.wheel.resolve())])
    steps.append(_run_step("F01-F13 foreign_machine_check", foreign_cmd))

    zd_cmd = [py, "-X", "utf8", str(HERE / "zero_dispatch_check.py")]
    if args.wheel:
        zd_cmd.extend(["--wheel", str(args.wheel.resolve())])
    steps.append(_run_step("zero_dispatch_check (13 surfaces)", zd_cmd))

    steps.append(_run_step("pytest setup/", [py, "-X", "utf8", "-m", "pytest", "setup", "-q"]))
    steps.append(_run_step("pytest skills/agent-compass/scripts/", [py, "-X", "utf8", "-m", "pytest", "skills/agent-compass/scripts", "-q"]))
    steps.append(_run_step("pytest tray/", [py, "-X", "utf8", "-m", "pytest", "tray", "-q"]))
    if USAGE_FLOAT_DIR.is_dir():
        steps.append(_run_step("pytest usage-float/", [py, "-X", "utf8", "-m", "pytest", "-q"], cwd=USAGE_FLOAT_DIR))

    if args.with_windows_sandbox:
        steps.append(_run_step("W01 windows_sandbox_check", [py, "-X", "utf8", str(HERE / "windows_sandbox_check.py"), "--connect"]))

    failed = [s for s in steps if s["returncode"] != 0]
    report_dir = args.report.resolve() / time.strftime("run-%Y%m%d-%H%M%S")
    report_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "overall": "PASS" if not failed else "FAIL",
        "failed_count": len(failed),
        "elapsed_seconds": round(time.time() - started, 1),
        "steps": steps,
    }
    (report_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = [
        f"# Release Verification Summary ({summary['overall']})",
        "",
        f"- Failed steps: **{len(failed)}** / {len(steps)}",
        f"- Total elapsed: **{summary['elapsed_seconds']}s**",
        "",
        "| Step | Status | Elapsed | RC |",
        "|---|---|---|---|",
    ]
    for s in steps:
        lines.append(f"| {s['name']} | {s['status']} | {s['elapsed_seconds']}s | {s['returncode']} |")
    (report_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (args.report.resolve() / "latest.txt").write_text(report_dir.name + "\n", encoding="utf-8")
    print(f"\nRelease verification: {summary['overall']} ({len(failed)} failed step(s)); report {report_dir / 'report.md'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
