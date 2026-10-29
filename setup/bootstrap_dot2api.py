"""Create dot2api storage and write caller credentials once. Prints fingerprints only."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE / 'tray'))
from runtime_paths import uv_path
from service_ports import get as service_port

ROOT = WORKSPACE / 'dot2api'
UV = uv_path()
HOME = Path.home() / ".dot2api"
DATA = HOME / "data"
CLIENT = HOME / "client.json"
CONNECT = HOME / "connect.json"

DOT_PORT = str(service_port("dot2api"))
ENV = os.environ.copy()
ENV.update(
    {
        "DOT2API_DATA_DIR": str(DATA),
        "DOT2API_HOST": "127.0.0.1",
        "DOT2API_PORT": DOT_PORT,
        "DOT2API_CAPABILITY_URLS": "1",
        "DOT2API_DOH": "1",
        "DOT2API_QUEUE": "dot",
        "DOT2API_COMPLETION_TIMEOUT": "900",
        "DOT2API_TASK_TTL": "7200",
    }
)


def run(args: list[str]) -> str:
    proc = subprocess.run(
        [str(UV), "run", "--no-sync", "dot2api", "--data-dir", str(DATA), *args],
        cwd=ROOT,
        env=ENV,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        sys.stderr.write(proc.stdout)
        raise SystemExit(proc.returncode)
    return proc.stdout


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def main() -> None:
    HOME.mkdir(exist_ok=True)
    if not (DATA / "encryption.key").exists():
        print(run(["init"]).strip())
    if CLIENT.exists():
        print(f"credentials already present at {CLIENT}")
        return
    payload = json.loads(run(["setup"]))
    CLIENT.write_text(
        json.dumps(
            {
                "base_url": f"http://127.0.0.1:{DOT_PORT}",
                "api_key": payload["api_key"],
                "queue": payload["queue"],
                "completion_timeout": 900,
                "task_ttl": 7200,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    CONNECT.write_text(
        json.dumps(
            {
                "queue": payload["queue"],
                "dot_token": payload["dot_token"],
                "public_base_url": "",
                "mcp_url": "",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {CLIENT}")
    print(f"wrote {CONNECT}")
    print(f"api_key_fp={fingerprint(payload['api_key'])}")
    print(f"dot_token_fp={fingerprint(payload['dot_token'])}")
    print(f"queue={payload['queue']}")


if __name__ == "__main__":
    main()
