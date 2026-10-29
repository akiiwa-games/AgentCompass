---
name: dot-subagent
description: Leaf-only runner for the local OpenAI Dot through dot2api. Use only after route-agent-work has chosen Dot. Covers the runner, packets, code written and run on Dot's own computer, and how data reaches it (public re-fetch, text packets, the private GitHub repository) and how results come back. No routing and no spawning children.
---

# Dot Subagent (leaf)

Leaf executor for the user's own Dot. The parent (`$route-agent-work` or the main agent)
owns routing, the outbound package, and integration. This leaf does not choose another model,
start another agent, or widen the assignment.

Without a router-owned dispatch, return `Status: blocked` before calling the runner or
transmitting content. The dispatch must name the outcome, the exact text the Dot may see, that the destination is the
user's own Dot, and, when data is handed over, the dataset. The standing authorization covers that packet. It does not
cover credentials, keys, or regulated or live financial data, and it does not cover any other destination. Standing policy
authorizes handing Dot explicitly designated data, never credentials (see [Getting data to Dot](#getting-data-to-dot)).


## Host dispatch availability

Before calling this runner directly, run `python -X utf8 scripts/host_policy.py --account dot`
from the installed sibling `route-agent-work` skill and require exit 0. It reads the optional
`~/.route-manager/host-policy.json` / `ROUTER_HOST_POLICY` override. A denied account or invalid
profile blocks this dispatch only; Self may continue other work. A recorded user constraint does
not bypass host availability. Router authorization is still required.

## Runner

The runner is `scripts/dot-subagent.cmd` (Windows) or `python -X utf8 scripts/run_dot_task.py`
in this skill's directory. It reads `~/.dot2api/client.json` unless `DOT2API_BASE_URL` and
`DOT2API_API_KEY` are both set. The client talks to loopback. It never prints the key.

```powershell
& <skill-dir>/scripts/dot-subagent.cmd doctor
& <skill-dir>/scripts/dot-subagent.cmd run --prompt-file <packet.md> --file <src1> --file <src2> --apply-to . --wait 1800
& <skill-dir>/scripts/dot-subagent.cmd submit --prompt-file <packet.md> --file <src1> --apply-to .
& <skill-dir>/scripts/dot-subagent.cmd status --task-id <id> --wait 120 --apply-to .
& <skill-dir>/scripts/dot-subagent.cmd run --task-id <id> --wait 600 --apply-to .
```

- `doctor` checks readiness and that the caller credential is accepted.
- `run` creates a task via `POST /v1/tasks` (prints `task_id=<id>` to stderr immediately so client timeouts or shell disconnects never cancel the running task!) and waits up to `--wait` seconds. Pass `--file <path>` (repeatable) to pack local workspace files automatically into the prompt, and `--apply-to <workspace_dir>` (or `--unpack-to <empty_dir>`) to append the `=== FILE: ... ===` return contract and apply returned files automatically when the task completes.
- `submit` (or `run --no-wait`) creates the task via `POST /v1/tasks`, prints `task_id=<id>` immediately (<10ms), and exits 0 without blocking.
- `status --task-id <id>` reads the task once without waiting (or waits up to `--wait <seconds>` if given) and prints `status`, `attempts` and the ages; pass `--apply-to <workspace_dir>` or `--output <reply.md>` to apply returned files as soon as `status` sees `completed`.

**Calling it from another harness's shell**:
- **One-command file edit / code task**:
  `python -X utf8 ~/.codex/skills/dot-subagent/scripts/run_dot_task.py run --prompt-file <packet.md> --file <path1> --file <path2> --apply-to . --wait 600`
- **Non-blocking submit + poll (when shell tool has a short timeout like 120s)**:
  1. `python -X utf8 ~/.codex/skills/dot-subagent/scripts/run_dot_task.py submit --prompt-file <packet.md> --file <path1> --apply-to .` -> prints `task_id=task_...` immediately.
  2. `python -X utf8 ~/.codex/skills/dot-subagent/scripts/run_dot_task.py status --task-id <id> --wait 90 --apply-to .` -> waits up to 90s per call without ever cancelling the task on timeout.

**Outcomes.** Exit 0: the reply is on stdout (or the `task_id` on `submit` / `--no-wait`). Exit 3: still open, stderr has `task_id=` and `OPEN status=… age=…`. Exit 1:
failed, stderr has `task_id=`, `FAILED status=… attempts=n/3` and then Dot's own message (it can be in Chinese; it says
what happened, so read it before resending). A failed task is already finished: resending creates a new task. **There is
no heartbeat**: a `running` task shows no progress and its `idle` age only grows, so the reply is the only proof of work.
A task that runs past its lease (about 1 hour) can be reclaimed, and the Dot's computer may be replaced meanwhile (see below).

The service accepts text only (images, files and any non-`text` part are refused with 400). Describe any file the Dot
must know about inside the prompt, and only when that text is authorized.

## Packet and return

Send the outcome, the facts that can change the answer, the allowed actions, and the shape of the reply. Tell the Dot to
do the research or operation itself and to return the result as the reply. Do not ask it to call back into this route.

Stdout is the reply text. Exit 0 means the Dot completed it. Exit 3 means the task is still open: stderr has the task id,
and a later `--task-id` read is the continuation. Any other exit is a failure. Do not treat a queued task or a webhook
delivery as a finished answer. Keep text packets bounded and return larger artifacts through the configured return channel.

## Code and tests on Dot's own computer

The router calls this work `self_contained_code`. Dot runs code on its own remote machine. Check the current
environment and ask it to report the exact commands, outputs and artifact hashes.

Ask for this reply shape, and give the spec, the interfaces and any existing source it must match, because it sees
nothing on this machine:

```
EXECUTED: yes|no
PYTHON: <python3 --version>
COMMAND: <the exact test command>
TEST OUTPUT:
<the complete, unedited output of the final run>
=== FILE: <relative/path> sha256=<hex from sha256sum after the last edit> ===
<exact file content, final newline included>
=== END FILE ===
NOTES: <anything that did not work>
```

Forbid markdown fences around the file blocks and anything between them. Say whether packages and downloads are allowed.
When the reply arrives:

```powershell
python -I -X utf8 <skill-dir>/scripts/unpack_files.py reply.md <new empty directory>
```

Exit 0 means every file matched its hash (the runner writes CRLF; the tool normalizes it, refuses unsafe paths and never
overwrites). A matching hash proves transfer integrity. Collect the worker's report, artifacts and checks already run.
The parent task and repository validation contract determine whether local review or additional tests are required.
Bring returned files into the repository when they are the deliverable, and review them under that contract. Binary or very long
content should be base64 (`base64 -w 76`). There is no way to edit files in the local repository, so existing files come
back whole or as a patch the parent applies.

## Remote environment preflight

Treat the remote machine and its files as temporary. Before a job, report the current OS,
Python and required package versions, available resources and a writable work root. Check
network access to the exact sources needed by the job instead of relying on an earlier run.
Both transfer generators share `scripts/remote_root.py`: `--root auto` probes remote
`DOT_WORK_ROOT`, or the current temporary directory, working directory and home, and verifies
writability with a real file. Set remote `DOT_WORK_ROOT` or pass `--root /absolute/path`
to reuse a specific root. Every packet reports `ROOT:`; reuse that reported path for later
packets and re-check every required file by size and hash. No path is guaranteed to persist.

Historical measurements and the retired tunnel relay are preserved locally under
`~/.route-manager/archive/dot-subagent-legacy-20261029/`; they describe past environments
and are not prerequisites or capacity guarantees for a current job.

## Getting data to Dot

Send only data the user designated, never credentials or keys (Dot refuses a key or token placed in a task). In order of
preference:

1. **Public data: Dot fetches it.** Verify reachability for this task and compare a slice hash with the local copy.
2. **Small local tables (up to a few MB): text packets.** `scripts/pack_chunks.py pack|check` splits a file into 36 KB
   packets. Check every part and the assembled file against the manifest; adjust packet size if the current channel rejects it.
3. **Larger local tables: the private repository `<owner>/dot-transfer`** (use your configured clone), read
   through Dot's connected GitHub connector. Dot cannot `git clone` it (no credentials) and the connector returns UTF-8
   text only, so the file travels encrypted (openssl AES-256, passphrase in the packet) as base64 text parts.

   ```powershell
   python -I -X utf8 <skill-dir>/scripts/github_send.py stage <file> $HOME/dot-transfer <dir>/job.md --id <job-name>
   #   -> job.md (one task), or job.fetch01.md ... job.assemble.md (send the fetch packets, then the assemble packet)
   python -I -X utf8 <skill-dir>/scripts/github_send.py check <dir>/job.md.json <every reply file>
   python -I -X utf8 <skill-dir>/scripts/github_send.py repacket <dir>/job.md.json <dir>/job2.md --root /absolute/reported/root
   ```

   `stage` makes 3 MB parts (about 10 MB failed), pushes them and writes the packets; each fetch packet skips parts already
   present with the right hash, so a failed or lost one is simply sent again; the assemble packet re-checks every part
   before it joins and decrypts. `--root auto` (default) has Dot discover and verify a writable directory in
   the remote environment and report it as `ROOT:`; `repacket` rebuilds the packets for a new root without touching GitHub,
   so there is no path patching by hand. The unpacked file stays on Dot's computer (`--cleanup` is for test transfers). The
   manifest holds the passphrase: keep it with the packets. Dot's own code passes the connector result to the shell in
   48,000-character chunks (no retyping, hashes exact); `fetch_file` returned an empty string every time so far, so the
   packet lists each blob id and says to call `fetch_blob` directly.

   Split larger transfers into bounded fetch packets and verify each reply before assembly.
   Git history keeps pushed blobs; keep only ciphertext there. When a job is over, delete its
   directory, and recreate the repository if the history must go.
4. **Raw multi-GB data does not fit.** Extract compact feature or event tables locally and send those.

## Getting results back from Dot

Keep the reply concise. Return full logs and larger artifacts through the configured private
repository on `dot-returns` (never `main`), written by Dot's own code through its GitHub connector.

```powershell
python -I -X utf8 <skill-dir>/scripts/github_return.py recipe <job-name>        # paste its output into the packet
python -I -X utf8 <skill-dir>/scripts/github_return.py fetch <reply.md> <new empty dir> --job <job-name>
python -I -X utf8 <skill-dir>/scripts/github_return.py purge <clone> <job-name>  # or delete returns/<job> through the API
```

Dot gzips each file, base64-encodes it, writes parts of at most 5,200 lines (about 400 KB of text) to
`returns/<job>/<name>.partNNN.b64.txt` and replies with one line per file: `FILE <name> <bytes> <sha256> parts=<n>
gzip=yes|no`. `fetch` downloads the parts with the local `gh` login, joins and decodes them, and writes a file only if its
size and sha256 match the line.

Set a timeout for each connector write and report partial progress when it expires. Verify
returned sizes and hashes locally. The branch lives in a private repository and git history
keeps what was pushed: return only authorized artifacts, then delete `returns/<job>` after fetch.

## Long jobs

Split transfers into independently verifiable fetch packets, then assemble only after all
expected sizes and hashes match. Treat remote files as a cache; retain the source and manifest
locally. For compute jobs, save results per unit of work, skip completed units on resume and
check a time budget between units. Set parallelism from the current preflight resources and
endpoint limits. Write full logs to artifacts and return a short progress summary plus hashes.
Use the runner's task ID, attempts and age to manage deadlines; a running status is not proof
that useful work is progressing.
