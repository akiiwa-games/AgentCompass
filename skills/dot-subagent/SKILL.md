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
cover credentials, keys, or regulated or live financial data, and it does not cover any other destination. The user also
approved (2026-10-09) handing Dot data they designate, never credentials (see [Getting data to Dot](#getting-data-to-dot)).


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
delivery as a finished answer. A reply of about 58,000 characters was accepted and no maximum is verified; about 49,000
characters in were carried exactly.

## Code and tests on Dot's own computer

The router calls this work `self_contained_code` (Dot first). Dot runs code on its own Linux virtual machine; on
2026-10-09 results matched a local recomputation exactly (a 200000-step SHA-256 chain, a seeded 3000-byte blob and its
base64, and the run of a 21-test `unittest` file). The only channel back is the reply text, so files come back inside it.

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
overwrites). A matching hash proves the transfer. Dot is an agent like any other worker (user, 2026-10-12): its reply and
its own test run are the result, so do not download its work to re-run or re-check it here. Bring files back only when
they are the deliverable that goes into this repository, and read them as you would any worker's change. Binary or very long
content should be base64 (`base64 -w 76`). There is no way to edit files in the local repository, so existing files come
back whole or as a patch the parent applies. A small module took about six minutes end to end.

## Dot's computer (measured 2026-10-09)

- Machine: Linux x86_64, 9 cores, about 10 GB RAM, no swap, Python 3.12.14 with numpy 2.3.5, pandas 2.2.3, scipy, httpx,
  psutil. `/`, `/tmp` and `$HOME` are RAM-backed tmpfs (4.9 GB each, `/` read-only). The only large writable disk is
  `/workspace/scratch/<id>/` (the id is chosen by the environment, e.g. `a1b2c3d4e5f6`: find it with `ls /workspace/scratch`;
  it is writable and a marker written in one task was read back by the next). **Nothing on disk is guaranteed to last.**
  `/workspace/shared` held files from 2026-09-29, then on 2026-10-09 around 22:24 (when a `/workspace/.scratch-cleaning`
  directory appeared) it became read-only (`mkdir` fails with errno 30) and everything in it was gone: three transferred
  files and the unpacked tools (see Long jobs). So every task that uses earlier files first checks that they are still there (size and hash) and says `MISSING` if
  not; every job puts its files in one directory named for the job and keeps the record of what was verified on this side.
- Packages: `pip install --user` fails (read-only home); `pip install --target <root>/pylib` works (public
  PyPI, 37 s for pyarrow, numba, pytest, requests, fsspec) and `PYTHONPATH=<root>/pylib` then imports them
  (`<root>` is the work root above; the install is lost with it, so keep it inside the job's directory);
  numba compiled and ran, a parquet round trip and pytest passed. Pin versions to the local venv when numbers must match.
- Network: egress is open to ordinary hosts (data.binance.vision, pypi.org, github.com, google.com, example.com and a bare
  IP all answered), except that `fapi.binance.com` answers 451 and `*.trycloudflare.com` answers 403. Anonymous
  `git clone` of a public repository works; git has no credential helper. A Vision monthly BTCUSDT 1m zip (1.8 MB)
  downloaded in 7 s and its sha256 matched the published `.CHECKSUM`.
- A single shell command ran 400 s without being cut off (upper limit unknown).
- The ChatGPT account's GitHub connector is available to Dot (it can read the user's private repositories as text).

## Getting data to Dot

Send only data the user designated, never credentials or keys (Dot refuses a key or token placed in a task). In order of
preference:

1. **Public data: Dot fetches it.** Compare a slice hash with the local copy. Reachability from the VM: Binance Vision
   (`aggTrades`, `metrics`, `bookDepth`, `fundingRate`), cninfo, csindex, Eastmoney datacenter-web and Sina finance pages
   answered 200; Dukascopy answered 429 (rate limit, back off); `push2his.eastmoney.com` 502 and `hq.sinajs.cn` 403.
2. **Small local tables (up to a few MB): text packets.** `scripts/pack_chunks.py pack|check` splits a file into 36 KB
   packets (about 45 KB is the safe maximum); 4 parts in parallel took 131 s and the assemble packet 73 s, every hash
   matched. Floats barely compress.
3. **Larger local tables: the private repository `<owner>/dot-transfer`** (a clone is at `~/dot-transfer`), read
   through Dot's connected GitHub connector. Dot cannot `git clone` it (no credentials) and the connector returns UTF-8
   text only, so the file travels encrypted (openssl AES-256, passphrase in the packet) as base64 text parts.

   ```powershell
   python -I -X utf8 <skill-dir>/scripts/github_send.py stage <file> $HOME/dot-transfer <dir>/job.md --id <job-name>
   #   -> job.md (one task), or job.fetch01.md ... job.assemble.md (send the fetch packets, then the assemble packet)
   python -I -X utf8 <skill-dir>/scripts/github_send.py check <dir>/job.md.json <every reply file>
   python -I -X utf8 <skill-dir>/scripts/github_send.py repacket <dir>/job.md.json <dir>/job2.md --root /workspace/scratch/<id>
   ```

   `stage` makes 3 MB parts (about 10 MB failed), pushes them and writes the packets; each fetch packet skips parts already
   present with the right hash, so a failed or lost one is simply sent again; the assemble packet re-checks every part
   before it joins and decrypts. `--root auto` (default) has Dot pick the first writable directory under
   `/workspace/scratch/` and report it as `ROOT:`; `repacket` rebuilds the packets for a new root without touching GitHub,
   so there is no path patching by hand. The unpacked file stays on Dot's computer (`--cleanup` is for test transfers). The
   manifest holds the passphrase: keep it with the packets. Dot's own code passes the connector result to the shell in
   48,000-character chunks (no retyping, hashes exact); `fetch_file` returned an empty string every time so far, so the
   packet lists each blob id and says to call `fetch_blob` directly.

   Measured 2026-10-09: 3 MB (4 MB of text) in about 96 s, 3.5 min as a single job; 61 parts / 134.6 MB took 13 min in one
   task (770 s); a 63-part task ran 61 minutes and then failed. The rate varies several times over, so plan 15 parts per
   task (about 3 to 5 minutes) and send the fetch packets in parallel. Git history keeps pushed blobs; keep only
   ciphertext there. When a job is over, delete its directory, and recreate the repository if the history must go.
4. **Raw multi-GB data does not fit.** Extract compact feature or event tables locally and send those.

`scripts/tunnel_send.py` (a one-shot encrypted download through a local Cloudflare quick tunnel) works locally but Dot
cannot reach `*.trycloudflare.com`; keep it only for another relay or host.

## Getting results back from Dot

The reply is typed by the model: about 18 s per 1,000 characters (Dot2API tasks 62 to 83), so 60,000 characters of log or
data cost about 18 minutes and the tail can be mistyped. Anything larger than a short summary should go back through the
same private repository instead, on the branch `dot-returns` (never `main`), written by Dot's own code through its
GitHub connector.

```powershell
python -I -X utf8 <skill-dir>/scripts/github_return.py recipe <job-name>        # paste its output into the packet
python -I -X utf8 <skill-dir>/scripts/github_return.py fetch <reply.md> <new empty dir> --job <job-name>
python -I -X utf8 <skill-dir>/scripts/github_return.py purge <clone> <job-name>  # or delete returns/<job> through the API
```

Dot gzips each file, base64-encodes it, writes parts of at most 5,200 lines (about 400 KB of text) to
`returns/<job>/<name>.partNNN.b64.txt` and replies with one line per file: `FILE <name> <bytes> <sha256> parts=<n>
gzip=yes|no`. `fetch` downloads the parts with the local `gh` login, joins and decodes them, and writes a file only if its
size and sha256 match the line.

Measured 2026-10-10: 300,000 random bytes (405 KB of text) written in 8.3 s and decoded to the exact sha256 in the file
name; a 2.5 MB CSV plus 600 KB of random bytes came back as 7 parts in 91 s with both hashes equal to a local
recomputation. A write of 1,000,000 bytes did not return in 120 s ("approval was pending"), so keep each write to the
400 KB part size; one write stuck for more than 50 minutes in a first test (task 86) and held one of Dot's task slots until
its lease ended, so the recipe tells Dot to stop after 60 s without a return and report the parts written. Writing needs
no token: it goes through the connector the user attached to the ChatGPT account. The branch lives in a private repository
and git history keeps what was pushed: send back only what the user is content to leave there, and delete `returns/<job>`
when it is fetched.

## Long jobs

What went wrong in the first big hand-off (an early strategy backtest packet; Dot2API task 37) and the rule
each failure gives:

| What happened | Rule |
| --- | --- |
| One task fetched 63 parts and ran 61 minutes. Its lease ran out, the task was reclaimed, `/workspace/shared` was replaced meanwhile, and it ended `failed` after 2 attempts with all files gone (Dot's message said the transfer and the 63 hashes had passed before that). | Keep a task to a few minutes. Split transfers into fetch packets (`github_send.py` does) and never let one task hold the only copy of the work. |
| The data of three earlier tasks (about 150 MB) vanished with no warning, while this skill said `/workspace/shared` persists. | Treat Dot's disk as a cache. The source of truth stays here, with hashes. Start every task that uses earlier files by listing them with size and hash (`MISSING` is a valid answer), before the expensive step. |
| `running` for an hour looked identical to working. | There is no heartbeat. Use `status --task-id` for `attempts` and `age`, set a deadline from the measured times, and decide at the deadline whether to wait or resend. |
| Packets embedded the old directory and passphrase, so a resend meant patching them with a script. | Generate packets from the manifest (`repacket --root`), never by editing text. |
| Hours of trial and error on the shell: PowerShell quoting, Windows paths, which exit code means failure. | Use the invocation under Runner and its exit codes; pass packets as files with absolute paths. |
| STATE.md and commit messages said the data was in place after it was gone. | Record a transfer as done only after `check` passed on the replies, with the root it reported. Re-check before relying on it later. |

What a day of long running showed (22 consecutive compute tasks, 9.1 hours, all completed on the
first attempt, gaps between tasks 10 to 170 s, 41 symbols rebuilt and hash-matched; Dot2API tasks 62 to 83):

- **The resumable pattern works; keep it.** One program on Dot with a DONE file per unit of work, skipped on the next run,
  a time budget checked between units, exit 3 for "more to do", and one task per run. Nothing was lost to the lease after
  task 37 (longest task 2,039 s against a lease of about 3,600 s).
- **Dot typing the log is a large share of each task.** The prompt asked for the complete stdout in the reply, 25,000 to
  68,000 characters. Across 21 tasks the duration was about 580 s plus 18 s per 1,000 characters of reply (correlation
  0.67), so a 60,000-character reply costs about 18 minutes of a 25-minute task. The largest reply is above the 58,000
  verified before. Have the program write the full log to a file on Dot and print a summary (one line per unit of work
  that finished, every hash, the final counts); return the full log or any data file through GitHub (see Getting results
  back) and read it only when a line looks wrong.
- **The budget is only checked between units.** "Stop after 16 minutes" ran 11 to 34 minutes, because a unit can be long and
  the reply takes time. Set the budget to about a third of the lease (10 to 12 minutes), not half.
- **Dot's shell is sometimes interrupted by an approval.** In 3 of 22 batch tasks (62, 70, 78) the run was killed with
  "network approval was cancelled" or "automatic permission approval review did not finish"; no exit status exists. Tasks 70
  and 78 survived because the packet allowed one retry of the same command; task 62 had no retry and delivered a partial
  batch. Make the program resumable (it was) and allow one retry in the packet. Writes can also fail ("exec-server transport
  disconnected", task 49): hash-and-skip packets cover it.
- **Downloads on Dot are slow per connection, so use many connections.** Measured 2026-10-10 against data.binance.vision
  (24 monthly zips, 44 MB, every checksum matched; another job was running at the same time): 1 thread 260 s (0.17 MB/s),
  8 threads 65 s, 16 threads 40 s. The batch program above fetched one file at a time, which is why a symbol (about
  80 monthly zips) took roughly 11 minutes of a 25-minute task. `scripts/parallel_fetch.py` (standard library, tested,
  `fetch_all(urls, threads=16)`, 404 returned as None, retries with a growing pause) is meant to be pasted into the
  packet; keep CPU work (parsing) in a process pool of at most 8, since the machine has 9 cores and the limit is per
  connection, not the CPU. Tasks do run side by side on Dot (tasks 86 and 87 ran together), so parallel work should also
  go in independent tasks when the pieces do not depend on each other.
- **`fetch_file` returned an empty string for every part, every time** (tasks 46 to 55), so each part cost a wasted call and
  a directory listing. Packets from `github_send.py` now list each part's git blob id and tell Dot to call `fetch_blob`
  directly.

Shape of a big job: (1) `stage` and send the fetch packets in parallel; (2) `check` their replies; (3) send the assemble
packet; (4) `check` all replies together; (5) only then send the compute task, whose first step lists the expected files
by size and hash. If the compute task is long, give it a result file per unit of work and ask it to skip units whose
result file exists, so a resend continues instead of restarting. Each stage is a separate small task that ends in a reply
you can verify here.
