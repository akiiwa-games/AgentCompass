"""Send one text task to the local Dot and print its reply. Never print credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import unpack_files

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_PENDING = 3

TASK_INSTRUCTIONS = (
    "This task is a chat completion request. payload.messages is the conversation, oldest "
    "first; each entry has a role and text content. Write the next assistant reply, honoring "
    "any system messages. Complete the task with the result "
    '{"content": "<your reply>"}. Reply with the answer only, without mentioning this task.'
)

FILE_RETURN_CONTRACT = """

## File Return Format (Required for Modified or Created Files)
For every file you create or modify, emit the complete file in your reply using this exact block format (compute the exact UTF-8 sha256 in Python first):

=== FILE: <relative/path> sha256=<64 lowercase hex> ===
<full file content>
=== END FILE ===
"""


def client_path() -> Path:
    return Path.home() / ".dot2api" / "client.json"


def load_config() -> dict:
    env_url = os.environ.get("DOT2API_BASE_URL", "").strip()
    env_key = os.environ.get("DOT2API_API_KEY", "").strip()
    if env_url or env_key:
        if not env_url or not env_key:
            raise RuntimeError("Set both DOT2API_BASE_URL and DOT2API_API_KEY, or neither.")
        file_config = {}
    else:
        path = client_path()
        if not path.is_file():
            raise RuntimeError(f"Missing {path}. Deploy dot2api before dispatching.")
        file_config = json.loads(path.read_text(encoding="utf-8"))
        env_url = str(file_config.get("base_url", "")).rstrip("/")
        env_key = str(file_config.get("api_key", ""))
    if not env_url or not env_key:
        raise RuntimeError("Dot client config needs base_url and api_key.")
    return {
        "base_url": env_url.rstrip("/"),
        "api_key": env_key,
        "queue": str(file_config.get("queue", "dot")),
        "completion_timeout": int(file_config.get("completion_timeout", 900)),
        "task_ttl": int(file_config.get("task_ttl", 7200)),
    }


def request(config: dict, method: str, path: str, body: dict | None, timeout: float):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        config["base_url"] + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def reply_text(result) -> str:
    if isinstance(result, dict):
        for key in ("content", "text", "output"):
            if isinstance(result.get(key), str):
                return result[key]
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)


def parse_sse(payload: bytes) -> tuple[str, str | None, str | None]:
    """Return reply text, task id, and a terminal error message."""
    text: list[str] = []
    task_id = None
    error = None
    for raw in payload.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if isinstance(event.get("id"), str):
            task_id = event["id"]
        err = event.get("error")
        if isinstance(err, dict) and "choices" not in event:
            error = str(err.get("message") or "Dot request failed")
            if isinstance(err.get("task_id"), str):
                task_id = err["task_id"]
            continue
        choices = event.get("choices")
        if isinstance(choices, list) and choices:
            delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                text.append(delta["content"])
    return "".join(text), task_id, error


def task_view(config: dict, task_id: str) -> dict:
    status, raw = request(config, "GET", f"/v1/tasks/{task_id}", None, 30)
    if status != 200:
        raise RuntimeError(f"Reading task failed with HTTP {status}")
    body = json.loads(raw.decode())
    task = body.get("task")
    if not isinstance(task, dict):
        raise RuntimeError("Task response has no task")
    return task


def seconds(value) -> float | None:
    """Epoch seconds from a number or an ISO-8601 string; None when it is neither."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def describe(task: dict, now: float | None = None) -> str:
    """One line: status, attempts and ages. A running task has no heartbeat, so its age is the only hint."""
    now = time.time() if now is None else now
    parts = [f"status={task.get('status')}", f"attempts={task.get('attempts')}/{task.get('max_attempts')}"]
    for label, key in (("age", "created_at"), ("idle", "updated_at")):
        moment = seconds(task.get(key))
        if moment is not None:
            parts.append(f"{label}={int(now - moment)}s")
    return " ".join(parts)


def pack_context_files(files: list[str] | None, base_dir: str | Path | None = None) -> str:
    """Format local files as === FILE: <rel_path> sha256=<hex> === blocks for the prompt."""
    if not files:
        return ""
    root = Path(base_dir).resolve() if base_dir else Path.cwd().resolve()
    sections = ["\n\n## Input Files\n"]
    for item in files:
        path = Path(item)
        full = path.resolve() if path.is_absolute() else (root / path).resolve()
        if not full.is_file():
            raise FileNotFoundError(f"Input file not found: {item}")
        try:
            rel = full.relative_to(root).as_posix()
        except ValueError:
            rel = full.name
        content = full.read_text(encoding="utf-8").replace("\r\n", "\n")
        if not content.endswith("\n"):
            content += "\n"
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        sections.append(f"=== FILE: {rel} sha256={digest} ===\n{content}=== END FILE ===\n")
    return "\n".join(sections)


def augment_prompt_with_files(
    prompt: str,
    files: list[str] | None = None,
    base_dir: str | Path | None = None,
    expect_files: bool = False,
) -> str:
    out = prompt
    packed = pack_context_files(files, base_dir=base_dir)
    if packed:
        out = out.rstrip() + packed
    if expect_files and "=== FILE:" not in prompt:
        out = out.rstrip() + "\n" + FILE_RETURN_CONTRACT
    return out


def handle_completed_reply(
    message: str,
    output: str | Path | None = None,
    unpack_to: str | Path | None = None,
    apply_to: str | Path | None = None,
) -> int:
    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(message, encoding="utf-8")
    if unpack_to:
        results = unpack_files.unpack(message, unpack_to, keep_unverified=False, allow_existing=False)
        for name, st in results:
            print(f"unpacked {name}: {st}", file=sys.stderr)
        if not results or not all(st == "ok" for _, st in results):
            print("ERROR: failed to unpack all verified file blocks", file=sys.stderr)
            return EXIT_FAILED
    if apply_to:
        results = unpack_files.unpack(message, apply_to, keep_unverified=True, allow_existing=True)
        for name, st in results:
            print(f"applied {name}: {st}", file=sys.stderr)
        if not results:
            print("ERROR: reply contained no === FILE: ... === blocks to apply", file=sys.stderr)
            return EXIT_FAILED
    return EXIT_OK


def submit_task_api(
    config: dict,
    messages: list[dict],
    idempotency_key: str | None = None,
) -> tuple[int, str | None, bytes]:
    """Create a task directly via POST /v1/tasks so client timeout/disconnect never cancels the task."""
    body: dict = {
        "queue": config.get("queue", "dot"),
        "instructions": TASK_INSTRUCTIONS,
        "payload": {"model": "dot", "messages": messages},
        "ttl_seconds": int(config.get("task_ttl", 7200)),
    }
    # dot2api requires idempotency_key on POST /v1/tasks (models.Submit); a fresh one per submission.
    body["idempotency_key"] = idempotency_key or f"task-{os.urandom(12).hex()}"
    status, raw = request(config, "POST", "/v1/tasks", body, 30)
    if status in (200, 201):
        try:
            parsed = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            parsed = {}
        task = parsed.get("task") if isinstance(parsed, dict) else None
        if isinstance(task, dict) and isinstance(task.get("id"), str):
            return status, task["id"], raw
    return status, None, raw


def wait_task(config: dict, task_id: str, deadline: float) -> tuple[int, str]:
    while True:
        task = task_view(config, task_id)
        status = task.get("status")
        if status == "completed":
            return EXIT_OK, reply_text(task.get("result"))
        if status not in ("queued", "running"):
            print(f"task_id={task_id}", file=sys.stderr)
            print(f"FAILED {describe(task)}", file=sys.stderr)
            return EXIT_FAILED, str(task.get("error") or f"Dot task {status}")
        if time.monotonic() >= deadline:
            print(f"task_id={task_id}", file=sys.stderr)
            print(f"OPEN {describe(task)}", file=sys.stderr)
            return EXIT_PENDING, "Dot has not finished; read this task id later (run --task-id, or status --task-id)."
        time.sleep(2)


def doctor(config: dict) -> int:
    try:
        with urllib.request.urlopen(config["base_url"] + "/readyz", timeout=5) as response:
            ready = response.status == 200
    except (OSError, urllib.error.URLError):
        print("status: failed")
        print(f"base_url: {config['base_url']}")
        print("ready: false")
        return EXIT_FAILED
    status, raw = request(config, "GET", "/v1/models", None, 15)
    model = ""
    if status == 200:
        body = json.loads(raw.decode())
        data = body.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            model = str(data[0].get("id", ""))
    print("status: ok" if ready and status == 200 else "status: failed")
    print(f"base_url: {config['base_url']}")
    print(f"ready: {str(ready).lower()}")
    print(f"auth: {'accepted' if status == 200 else 'rejected'}")
    if model:
        print(f"model: {model}")
    return EXIT_OK if ready and status == 200 else EXIT_FAILED


def status_command(
    config: dict,
    task_id: str,
    wait: float = 0.0,
    output: str | Path | None = None,
    unpack_to: str | Path | None = None,
    apply_to: str | Path | None = None,
) -> int:
    """Read a task (or wait up to `wait` seconds if > 0): exit 0 completed, 3 still open, 1 failed."""
    if wait and wait > 0:
        code, message = wait_task(config, task_id, time.monotonic() + wait)
        if code == EXIT_OK:
            print(message)
            return handle_completed_reply(message, output=output, unpack_to=unpack_to, apply_to=apply_to)
        print(message, file=sys.stderr)
        return code
    task = task_view(config, task_id)
    print(f"task_id={task_id}")
    print(describe(task))
    state = task.get("status")
    if state == "completed":
        if output or unpack_to or apply_to:
            message = reply_text(task.get("result"))
            return handle_completed_reply(message, output=output, unpack_to=unpack_to, apply_to=apply_to)
        return EXIT_OK
    if state in ("queued", "running"):
        print("No heartbeat: running does not prove progress. Dot's reply is the only proof.")
        return EXIT_PENDING
    print(str(task.get("error") or f"Dot task {state}"))
    return EXIT_FAILED


def run_task(
    config: dict,
    prompt: str,
    system: str | None,
    task_id: str | None,
    wait: float,
    no_wait: bool = False,
    idempotency_key: str | None = None,
    output: str | Path | None = None,
    unpack_to: str | Path | None = None,
    apply_to: str | Path | None = None,
) -> int:
    deadline = time.monotonic() + wait
    if task_id:
        code, message = wait_task(config, task_id, deadline)
        if code == EXIT_OK:
            print(message)
            return handle_completed_reply(message, output=output, unpack_to=unpack_to, apply_to=apply_to)
        print(message, file=sys.stderr)
        return code
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    status, created_id, raw = submit_task_api(config, messages, idempotency_key=idempotency_key)
    if created_id:
        print(f"task_id={created_id}", file=sys.stderr)
        if no_wait or wait <= 0:
            print(created_id)
            return EXIT_OK
        code, message = wait_task(config, created_id, deadline)
        if code == EXIT_OK:
            print(message)
            return handle_completed_reply(message, output=output, unpack_to=unpack_to, apply_to=apply_to)
        print(message, file=sys.stderr)
        return code
    if status in (404, 405):
        status, raw = request(
            config,
            "POST",
            "/v1/chat/completions",
            {"model": "dot", "stream": True, "messages": messages},
            max(10.0, min(wait + 15.0, float(config["completion_timeout"] + 60))),
        )
        if status == 200:
            reply, found_id, error = parse_sse(raw)
            if error:
                if found_id and "not answered" in error.lower():
                    print(f"task_id={found_id}", file=sys.stderr)
                    code, message = wait_task(config, found_id, deadline)
                    if code == EXIT_OK:
                        print(message)
                        return handle_completed_reply(message, output=output, unpack_to=unpack_to, apply_to=apply_to)
                    print(message, file=sys.stderr)
                    return code
                print(error, file=sys.stderr)
                return EXIT_FAILED
            print(reply)
            return handle_completed_reply(reply, output=output, unpack_to=unpack_to, apply_to=apply_to)
    try:
        body = json.loads(raw.decode() or "{}")
    except json.JSONDecodeError:
        body = {}
    err = body.get("error") if isinstance(body, dict) else None
    message = err.get("message") if isinstance(err, dict) else f"HTTP {status}"
    found = err.get("task_id") if isinstance(err, dict) else None
    if status == 504 and isinstance(found, str):
        print(f"task_id={found}", file=sys.stderr)
        code, pending = wait_task(config, found, deadline)
        if code == EXIT_OK:
            print(pending)
            return handle_completed_reply(pending, output=output, unpack_to=unpack_to, apply_to=apply_to)
        print(pending, file=sys.stderr)
        return code
    print(str(message), file=sys.stderr)
    return EXIT_FAILED


def add_task_io_args(subparser: argparse.ArgumentParser, include_prompt: bool = True) -> None:
    if include_prompt:
        subparser.add_argument("--prompt", default="")
        subparser.add_argument("--prompt-file")
        subparser.add_argument("--system")
        subparser.add_argument("--file", dest="files", action="append", default=[],
                               help="Pack a local file into the prompt (repeatable).")
        subparser.add_argument("--base-dir", help="Base directory for relative --file paths (default: cwd).")
        subparser.add_argument("--expect-files", action="store_true",
                               help="Append the === FILE: ... === return format contract to the prompt.")
        subparser.add_argument("--idempotency-key", help="Optional idempotency key for POST /v1/tasks.")
    subparser.add_argument("--output", help="Write the completed assistant reply to this file path.")
    subparser.add_argument("--unpack-to", help="Unpack verified === FILE: ... === blocks into this empty directory.")
    subparser.add_argument("--apply-to", help="Unpack === FILE: ... === blocks directly into this existing workspace directory.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    status = commands.add_parser("status")
    status.add_argument("--task-id", required=True)
    status.add_argument("--wait", type=float, default=0.0,
                        help="Optional seconds to wait for completion before returning.")
    add_task_io_args(status, include_prompt=False)
    submit = commands.add_parser("submit", help="Submit a task via POST /v1/tasks, print task_id immediately, and exit 0.")
    add_task_io_args(submit, include_prompt=True)
    run = commands.add_parser("run")
    add_task_io_args(run, include_prompt=True)
    run.add_argument("--task-id")
    run.add_argument("--wait", type=float)
    run.add_argument("--no-wait", action="store_true",
                     help="Submit the task and return immediately after printing task_id.")
    args = parser.parse_args()
    try:
        config = load_config()
    except (OSError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
        print(f"Status: blocked\n{exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.command == "doctor":
        return doctor(config)
    if args.command == "status":
        try:
            return status_command(
                config,
                args.task_id,
                wait=args.wait,
                output=args.output,
                unpack_to=args.unpack_to,
                apply_to=args.apply_to,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"FAILED reading the task: {exc}", file=sys.stderr)
            return EXIT_FAILED
    task_id = getattr(args, "task_id", None)
    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    elif not prompt and not task_id and not sys.stdin.isatty():
        prompt = sys.stdin.read()
    if not task_id and not prompt.strip():
        print("A prompt or --task-id is required.", file=sys.stderr)
        return EXIT_USAGE
    if not task_id:
        try:
            expect_files = bool(args.expect_files or args.unpack_to or args.apply_to)
            prompt = augment_prompt_with_files(
                prompt,
                files=args.files,
                base_dir=args.base_dir or args.apply_to,
                expect_files=expect_files,
            )
        except (OSError, ValueError) as exc:
            print(f"Status: blocked\n{exc}", file=sys.stderr)
            return EXIT_USAGE
    if args.command == "submit":
        return run_task(
            config,
            prompt,
            args.system,
            None,
            0.0,
            no_wait=True,
            idempotency_key=args.idempotency_key,
        )
    wait = args.wait if args.wait is not None else float(config["task_ttl"])
    return run_task(
        config,
        prompt,
        args.system,
        task_id,
        wait,
        no_wait=args.no_wait,
        idempotency_key=args.idempotency_key,
        output=args.output,
        unpack_to=args.unpack_to,
        apply_to=args.apply_to,
    )


if __name__ == "__main__":
    raise SystemExit(main())
