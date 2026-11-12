"""Post-completion ground-truth telemetry for completed dispatches.

Records actual outcomes on completed rows in `dispatch.sqlite3` so classifier
hours, complexity and consumption estimates can be calibrated against real runs.
Quota economy and allocation modules never import or read these fields.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess

import provider_inventory

_PASEO_MSG_ID_RE = re.compile(r'<paseo-system\b[^>]*?\bmessage-id="([^"]+)"')
_CODEX_ROLLOUT_ID_RE = re.compile(r'^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(.+)$')
_CODEX_ENV_INJECTION_PREFIXES = (
    '<environment_context>',
    '<permissions instructions>',
    '<collaboration_mode>',
    '# AGENTS.md instructions',
)
_KNOWN_HARNESSES = frozenset({
    'claude',
    'codex',
    'antigravity',
    'pi',
    'grok-bot',
    'dot',
    'opencode',
    'omp',
    'grok',
    'chatgpt-web',
})


@dataclass
class _TranscriptEvent:
    ts_epoch: float | None
    ts_iso: str | None
    is_user_prompt: bool
    paseo_ids: tuple[str, ...]
    claude_usage: tuple[str, dict] | None = None
    codex_token_info: dict | None = None


def _parse_iso(value: object) -> tuple[float | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, None
    raw = value.strip()
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return None, None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp(), raw


def _is_int_num(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


_HEX_OID_RE = re.compile(r'^[0-9a-fA-F]{40,64}$')


def _git(cwd: str | Path, *args: str, timeout: float = 5.0) -> subprocess.CompletedProcess[str] | None:
    env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'LC_ALL': 'C'}
    kwargs: dict = {
        'stdin': subprocess.DEVNULL,
        'capture_output': True,
        'text': True,
        'encoding': 'utf-8',
        'errors': 'replace',
        'timeout': timeout,
        'env': env,
    }
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.run(['git', '-C', str(cwd), *args], **kwargs)
    except (OSError, subprocess.SubprocessError):
        return None


def _locate_git_dirs(target: Path) -> tuple[Path, Path, Path] | None:
    try:
        resolved = target.resolve()
    except OSError:
        resolved = target
    for cur in (resolved, *resolved.parents):
        dot_git = cur / '.git'
        try:
            is_dir = dot_git.is_dir()
            is_file = False if is_dir else dot_git.is_file()
        except OSError:
            continue
        if is_dir:
            gitdir = dot_git
        elif is_file:
            try:
                first_line = next(
                    (ln.strip() for ln in dot_git.read_text(encoding='utf-8', errors='replace').splitlines() if ln.strip()),
                    '',
                )
            except OSError:
                continue
            if not first_line.startswith('gitdir:'):
                continue
            raw_gitdir = first_line[7:].strip()
            if not raw_gitdir:
                continue
            gd_path = Path(raw_gitdir)
            try:
                gitdir = (gd_path if gd_path.is_absolute() else (cur / gd_path)).resolve()
            except OSError:
                continue
            if not gitdir.is_dir():
                continue
        else:
            continue
        if not (gitdir / 'HEAD').is_file():
            continue
        commondir = gitdir
        cd_file = gitdir / 'commondir'
        if cd_file.is_file():
            try:
                raw_cd = cd_file.read_text(encoding='utf-8', errors='replace').strip()
                if raw_cd:
                    cd_path = Path(raw_cd)
                    commondir = (cd_path if cd_path.is_absolute() else (gitdir / cd_path)).resolve()
            except OSError:
                commondir = gitdir
        return cur, gitdir, commondir
    return None


def _lookup_packed_ref(commondir: Path, refname: str) -> str | None:
    packed_path = commondir / 'packed-refs'
    if not packed_path.is_file():
        return None
    matched_oid: str | None = None
    try:
        with open(packed_path, encoding='utf-8', errors='replace') as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith('#'):
                    continue
                if matched_oid is not None:
                    if line.startswith('^'):
                        peeled = line[1:].strip()
                        if _HEX_OID_RE.match(peeled):
                            return peeled.lower()
                    return matched_oid
                parts = line.split(' ', 1)
                if len(parts) == 2 and parts[1].strip() == refname and _HEX_OID_RE.match(parts[0]):
                    matched_oid = parts[0].lower()
    except OSError:
        return None
    return matched_oid


def _resolve_git_head_oid(target: Path, gitdir: Path, commondir: Path) -> tuple[str | None, str | None]:
    ref = 'HEAD'
    for _ in range(10):
        candidates = (gitdir / 'HEAD',) if ref == 'HEAD' else (gitdir / ref, commondir / ref)
        content: str | None = None
        for cand in candidates:
            try:
                if cand.is_file():
                    content = cand.read_text(encoding='utf-8', errors='replace').strip()
                    break
            except OSError:
                continue
        if content is None:
            packed = _lookup_packed_ref(commondir, ref)
            if packed:
                return packed, None
            if (gitdir / 'reftable').is_dir() or (commondir / 'reftable').is_dir():
                head = _git(target, 'rev-parse', '--verify', 'HEAD^{commit}')
                if head is None:
                    return None, 'git_unavailable'
                if head.returncode != 0 or not head.stdout.strip():
                    return None, 'git_head_unborn'
                return head.stdout.strip().lower(), None
            return None, 'git_head_unborn'
        if content.startswith('ref:'):
            nxt = content[4:].strip()
            if not nxt or '..' in Path(nxt).parts:
                return None, 'git_head_unborn'
            ref = nxt
            continue
        if _HEX_OID_RE.match(content):
            return content.lower(), None
        return None, 'git_head_unborn'
    return None, 'git_head_unborn'


def capture_launch_git(cwd: str | None, *, external_runner: bool = False) -> dict[str, str | None]:
    """Capture git HEAD SHA and worktree toplevel in `cwd` at dispatch launch.

    Reads `.git` metadata directly on disk so `registry.prepare` does not spawn
    child processes on every dispatch, falling back to `git rev-parse` only for
    `reftable` repositories.
    """
    if not isinstance(cwd, str) or not cwd.strip():
        reason = 'external_remote_runner' if external_runner else 'worker_cwd_unknown'
        return {'launch_commit': None, 'launch_git_toplevel': None, 'launch_commit_reason': reason}
    target = Path(cwd)
    if not target.is_dir():
        return {'launch_commit': None, 'launch_git_toplevel': None, 'launch_commit_reason': 'worker_cwd_missing'}
    located = _locate_git_dirs(target)
    if located is None:
        return {'launch_commit': None, 'launch_git_toplevel': None, 'launch_commit_reason': 'not_a_git_checkout'}
    toplevel_path, gitdir, commondir = located
    toplevel = str(toplevel_path)
    oid, err_reason = _resolve_git_head_oid(target, gitdir, commondir)
    if oid is None:
        return {'launch_commit': None, 'launch_git_toplevel': toplevel, 'launch_commit_reason': err_reason or 'git_head_unborn'}
    return {
        'launch_commit': oid,
        'launch_git_toplevel': toplevel,
        'launch_commit_reason': None,
    }


def _null_git_changes(reason: str) -> dict[str, int | str | None]:
    return {
        'files_changed': None,
        'lines_added': None,
        'lines_deleted': None,
        'lines_changed': None,
        'git_diff_basis': None,
        'git_changes_reason': reason,
    }


def collect_git_changes(row: dict) -> dict[str, int | str | None]:
    """Count files and lines changed in the worker's `cwd` relative to `launch_commit`.

    Uses `git diff --no-ext-diff --numstat <launch_commit> --` against the worker's
    working tree so committed, staged and unstaged tracked changes since launch are
    included while pre-existing untracked files in shared directories are ignored.
    """
    cwd = row.get('cwd')
    external = row.get('dispatch_mode') in ('grok-bot', 'dot')
    if not isinstance(cwd, str) or not cwd.strip():
        reason = 'external_remote_runner' if external else (row.get('launch_commit_reason') or 'worker_cwd_unknown')
        return _null_git_changes(reason)
    target = Path(cwd)
    if not target.is_dir():
        return _null_git_changes('worker_cwd_missing')
    top = _git(target, 'rev-parse', '--show-toplevel')
    if top is None:
        return _null_git_changes('git_unavailable')
    if top.returncode != 0 or not top.stdout.strip():
        return _null_git_changes('not_a_git_checkout')
    launch_commit = row.get('launch_commit')
    if not isinstance(launch_commit, str) or not launch_commit.strip():
        return _null_git_changes(row.get('launch_commit_reason') or 'launch_commit_not_recorded')
    commit = launch_commit.strip()
    verified = _git(target, 'cat-file', '-e', f'{commit}^{{commit}}')
    if verified is None:
        return _null_git_changes('git_unavailable')
    if verified.returncode != 0:
        return _null_git_changes('launch_commit_unreachable_in_cwd')
    diff = _git(target, '-c', 'core.quotepath=false', 'diff', '--no-ext-diff', '--numstat', commit, '--', timeout=10.0)
    if diff is None or diff.returncode != 0:
        return _null_git_changes('git_diff_failed')
    files_changed = 0
    lines_added = 0
    lines_deleted = 0
    for line in diff.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split('\t', 2)
        if len(parts) < 3:
            continue
        files_changed += 1
        if parts[0] != '-':
            try:
                lines_added += int(parts[0])
            except ValueError:
                return _null_git_changes('git_diff_parse_error')
        if parts[1] != '-':
            try:
                lines_deleted += int(parts[1])
            except ValueError:
                return _null_git_changes('git_diff_parse_error')
    return {
        'files_changed': files_changed,
        'lines_added': lines_added,
        'lines_deleted': lines_deleted,
        'lines_changed': lines_added + lines_deleted,
        'git_diff_basis': 'working_tree_vs_launch_commit',
        'git_changes_reason': None,
    }


def _read_paseo_providers(paseo_home: Path) -> dict[str, dict]:
    cfg_path = paseo_home / 'config.json'
    try:
        doc = json.loads(cfg_path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}
    providers = (doc.get('agents') or {}).get('providers') if isinstance(doc, dict) else None
    return providers if isinstance(providers, dict) else {}


def _follow_paseo_extends(provider: str | None, providers: dict[str, dict]) -> tuple[str | None, dict[str, str]]:
    if not isinstance(provider, str) or not provider.strip():
        return None, {}
    cur = provider.strip()
    env: dict[str, str] = {}
    for _ in range(len(providers) + 1):
        spec = providers.get(cur) if isinstance(providers.get(cur), dict) else {}
        raw_env = spec.get('env')
        if isinstance(raw_env, dict):
            env = {**{str(k): str(v) for k, v in raw_env.items() if isinstance(v, str)}, **env}
        nxt = spec.get('extends')
        if not isinstance(nxt, str) or not nxt.strip():
            break
        cur = nxt.strip()
    return cur, env


def _normalize_harness_name(raw: str | None) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    val = raw.strip().lower()
    if val == 'antigravity-resilient':
        return 'antigravity'
    if val in _KNOWN_HARNESSES:
        return val
    family = provider_inventory.launcher_family(val)
    if family == 'antigravity-resilient':
        return 'antigravity'
    if family in _KNOWN_HARNESSES:
        return family
    acct_family = provider_inventory.account_family(val)
    if acct_family == 'antigravity-resilient':
        return 'antigravity'
    if acct_family in _KNOWN_HARNESSES:
        return acct_family
    return None


def _resolve_harness(row: dict, agent_doc: dict | None, paseo_home: Path) -> tuple[str | None, dict[str, str]]:
    mode = row.get('dispatch_mode')
    if mode in ('grok-bot', 'dot', 'chatgpt-web'):
        return str(mode), {}
    providers = _read_paseo_providers(paseo_home)
    bundle = row.get('bundle') if isinstance(row.get('bundle'), dict) else {}
    provider = bundle.get('provider') if isinstance(bundle.get('provider'), str) else None
    paseo_root, provider_env = _follow_paseo_extends(provider, providers)

    persistence = (agent_doc.get('persistence') if isinstance(agent_doc, dict) else None) or {}
    persisted_provider = persistence.get('provider') if isinstance(persistence.get('provider'), str) else None
    persisted_root, persisted_env = _follow_paseo_extends(persisted_provider, providers)
    if persisted_env:
        provider_env = {**provider_env, **persisted_env}

    for candidate in (
        persisted_root,
        persisted_provider,
        bundle.get('harness'),
        paseo_root,
        provider,
        bundle.get('account'),
    ):
        resolved = _normalize_harness_name(candidate if isinstance(candidate, str) else None)
        if resolved:
            return resolved, provider_env
    return None, provider_env


def _unsupported_harness_reason(harness: str | None) -> str:
    if harness == 'antigravity':
        return 'antigravity_session_telemetry_unsupported'
    if harness == 'pi':
        return 'pi_session_telemetry_unsupported'
    if harness == 'grok-bot':
        return 'grok_bot_remote_telemetry_unavailable'
    if harness == 'dot':
        return 'dot_remote_telemetry_unavailable'
    if harness:
        return f'{harness}_session_telemetry_unsupported'
    return 'harness_unknown'


def _null_session_telemetry(
    harness: str | None,
    reason: str,
    *,
    session_id: str | None = None,
    session_reason: str | None = None,
) -> dict:
    return {
        'telemetry_harness': harness,
        'worker_session_id': session_id,
        'worker_session_reason': session_reason or (None if session_id else reason),
        'active_seconds': None,
        'idle_threshold_seconds': None,
        'active_seconds_reason': reason,
        'token_usage': None,
        'token_usage_reason': reason,
        'initial_active_seconds': None,
        'followup_active_seconds': None,
        'first_followup_at': None,
        'user_turns': None,
        'followup_turns': None,
        'followup_boundary_reason': reason,
    }


def _candidate_provider_roots(
    harness: str,
    row: dict,
    provider_env: dict[str, str],
    home: Path | None,
    paseo_home: Path,
) -> list[Path]:
    bundle = row.get('bundle') if isinstance(row.get('bundle'), dict) else {}
    provider = (bundle.get('provider') or '').strip()
    account = (bundle.get('account') or '').strip()

    raw_paths: list[Path] = []
    env_key = 'CLAUDE_CONFIG_DIR' if harness == 'claude' else 'CODEX_HOME'
    if provider_env.get(env_key):
        raw_paths.append(Path(provider_env[env_key]).expanduser())

    try:
        discovered_accounts = provider_inventory.current().get('accounts') or {}
    except Exception:
        discovered_accounts = {}
    for key in (provider, account):
        if key and isinstance(discovered_accounts.get(key), dict):
            acct_home = discovered_accounts[key].get('home')
            if isinstance(acct_home, str) and acct_home.strip():
                raw_paths.append(Path(acct_home).expanduser())

    bases: list[Path] = []
    if home is not None:
        bases.append(Path(home))
    if os.environ.get('ROUTER_PROVIDER_HOME'):
        bases.append(Path(os.environ['ROUTER_PROVIDER_HOME']))
    if paseo_home.name == '.paseo':
        bases.append(paseo_home.parent)
    bases.append(Path.home())

    for base in bases:
        if provider and provider != harness:
            raw_paths.append(base / f'.{provider}')
        if account and account not in (provider, harness):
            raw_paths.append(base / f'.{account}')
        raw_paths.append(base / f'.{harness}')

    out: list[Path] = []
    seen: set[str] = set()
    for candidate in raw_paths:
        norm = os.path.normcase(os.path.normpath(str(candidate)))
        if norm in seen:
            continue
        seen.add(norm)
        if candidate.is_dir():
            out.append(candidate)
    return out


def _locate_transcript(
    harness: str,
    roots: list[Path],
    session_id: str | None,
    dispatch_id: str | None,
    launched_at: str | None,
) -> tuple[Path | None, str | None]:
    if session_id:
        patterns = (
            (f'projects/*/{session_id}.jsonl',)
            if harness == 'claude'
            else (f'sessions/**/rollout-*-{session_id}.jsonl', f'archived_sessions/rollout-*-{session_id}.jsonl')
        )
        for root in roots:
            for pattern in patterns:
                found = next(root.glob(pattern), None)
                if found and found.is_file():
                    return found, session_id

    if not isinstance(dispatch_id, str) or not dispatch_id.strip():
        return None, session_id
    did = dispatch_id.strip()
    launched_epoch, _ = _parse_iso(launched_at)
    min_mtime = (launched_epoch - 120.0) if launched_epoch is not None else None

    scan_patterns = (
        ('projects/*/*.jsonl',)
        if harness == 'claude'
        else ('sessions/**/rollout-*.jsonl', 'archived_sessions/rollout-*.jsonl')
    )
    candidates: list[tuple[float, Path]] = []
    for root in roots:
        for pattern in scan_patterns:
            for path in root.glob(pattern):
                try:
                    st = path.stat()
                except OSError:
                    continue
                candidates.append((st.st_mtime, path))
    candidates.sort(key=lambda item: item[0], reverse=True)

    ordered_groups = (
        (
            [path for mtime, path in candidates if mtime >= min_mtime],
            [path for mtime, path in candidates if mtime < min_mtime],
        )
        if min_mtime is not None
        else ([path for _, path in candidates], [])
    )
    for group in ordered_groups:
        for path in group:
            try:
                with open(path, encoding='utf-8', errors='replace') as handle:
                    for line in handle:
                        if did not in line:
                            continue
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue
                        if did in _extract_obj_paseo_ids(harness, obj):
                            inferred = _infer_session_id(harness, path)
                            return path, session_id or inferred
            except OSError:
                continue
    return None, session_id


def _infer_session_id(harness: str, path: Path) -> str:
    if harness == 'claude':
        return path.stem
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            for _ in range(5):
                line = handle.readline()
                if not line:
                    break
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get('type') == 'session_meta':
                    meta_id = (obj.get('payload') or {}).get('id')
                    if isinstance(meta_id, str) and meta_id.strip():
                        return meta_id.strip()
    except (OSError, ValueError):
        pass
    m = _CODEX_ROLLOUT_ID_RE.match(path.stem)
    return m.group(1) if m else path.stem


def _extract_paseo_ids(text: str) -> tuple[str, ...]:
    if '<paseo-system' not in text:
        return ()
    return tuple(dict.fromkeys(_PASEO_MSG_ID_RE.findall(text)))


def _claude_user_prompt_text(obj: dict) -> str | None:
    if obj.get('type') != 'user':
        return None
    if obj.get('isMeta') or obj.get('isCompactSummary') or obj.get('isVisibleInTranscriptOnly'):
        return None
    message = obj.get('message')
    if not isinstance(message, dict):
        return None
    content = message.get('content')
    if isinstance(content, str):
        stripped = content.strip()
        return stripped if stripped else None
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content):
            return None
        texts = [
            b['text']
            for b in content
            if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str) and b['text'].strip()
        ]
        return '\n'.join(texts) if texts else None
    return None


def _codex_response_user_text(payload: dict) -> str | None:
    if payload.get('type') != 'message' or payload.get('role') != 'user':
        return None
    content = payload.get('content')
    if not isinstance(content, list):
        return None
    texts = [
        b['text']
        for b in content
        if isinstance(b, dict)
        and b.get('type') in ('input_text', 'text')
        and isinstance(b.get('text'), str)
        and b['text'].strip()
    ]
    if not texts:
        return None
    joined = '\n'.join(texts).strip()
    if any(joined.startswith(prefix) for prefix in _CODEX_ENV_INJECTION_PREFIXES):
        return None
    return joined


def _extract_obj_paseo_ids(harness: str, obj: dict) -> tuple[str, ...]:
    if not isinstance(obj, dict):
        return ()
    if harness == 'claude':
        text = _claude_user_prompt_text(obj)
        return _extract_paseo_ids(text) if text else ()
    payload = obj.get('payload') if isinstance(obj.get('payload'), dict) else {}
    if obj.get('type') == 'event_msg' and payload.get('type') == 'user_message':
        msg = payload.get('message')
        return _extract_paseo_ids(msg) if isinstance(msg, str) else ()
    if obj.get('type') == 'response_item':
        text = _codex_response_user_text(payload)
        return _extract_paseo_ids(text) if text else ()
    return ()


def _parse_claude_transcript(path: Path) -> list[_TranscriptEvent]:
    events: list[_TranscriptEvent] = []
    with open(path, encoding='utf-8', errors='replace') as handle:
        for line_no, line in enumerate(handle, 1):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            kind = obj.get('type')
            ts_epoch, ts_iso = _parse_iso(obj.get('timestamp')) if kind in ('user', 'assistant') else (None, None)
            user_text = _claude_user_prompt_text(obj)
            is_prompt = user_text is not None
            paseo_ids = _extract_paseo_ids(user_text) if user_text else ()

            claude_usage = None
            if kind == 'assistant':
                msg = obj.get('message') if isinstance(obj.get('message'), dict) else {}
                model = msg.get('model')
                usage = msg.get('usage')
                if isinstance(usage, dict) and not (isinstance(model, str) and model.startswith('<')):
                    msg_id = str(msg.get('id') or obj.get('uuid') or f'line:{line_no}')
                    claude_usage = (msg_id, usage)

            if ts_epoch is not None or is_prompt or claude_usage is not None:
                events.append(
                    _TranscriptEvent(
                        ts_epoch=ts_epoch,
                        ts_iso=ts_iso,
                        is_user_prompt=is_prompt,
                        paseo_ids=paseo_ids,
                        claude_usage=claude_usage,
                    )
                )
    return events


def _parse_codex_transcript(path: Path) -> list[_TranscriptEvent]:
    raw_objs: list[dict] = []
    has_event_user_msg = False
    with open(path, encoding='utf-8', errors='replace') as handle:
        for line in handle:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            raw_objs.append(obj)
            payload = obj.get('payload') if isinstance(obj.get('payload'), dict) else {}
            if obj.get('type') == 'event_msg' and payload.get('type') == 'user_message':
                has_event_user_msg = True

    events: list[_TranscriptEvent] = []
    for obj in raw_objs:
        ts_epoch, ts_iso = _parse_iso(obj.get('timestamp'))
        payload = obj.get('payload') if isinstance(obj.get('payload'), dict) else {}
        is_prompt = False
        paseo_ids: tuple[str, ...] = ()

        if obj.get('type') == 'event_msg' and payload.get('type') == 'user_message':
            msg = payload.get('message')
            if isinstance(msg, str) and msg.strip():
                is_prompt = True
                paseo_ids = _extract_paseo_ids(msg)
        elif obj.get('type') == 'response_item':
            resp_text = _codex_response_user_text(payload)
            if resp_text:
                if not has_event_user_msg:
                    is_prompt = True
                paseo_ids = _extract_paseo_ids(resp_text)

        token_info = None
        if obj.get('type') == 'event_msg' and payload.get('type') == 'token_count':
            info = payload.get('info')
            if isinstance(info, dict):
                token_info = info

        if ts_epoch is not None or is_prompt or paseo_ids or token_info is not None:
            events.append(
                _TranscriptEvent(
                    ts_epoch=ts_epoch,
                    ts_iso=ts_iso,
                    is_user_prompt=is_prompt,
                    paseo_ids=paseo_ids,
                    codex_token_info=token_info,
                )
            )
    return events


def _slice_events_for_dispatch(
    events: list[_TranscriptEvent],
    row: dict,
    other_dispatch_ids: set[str],
) -> tuple[int, int] | None:
    if not events:
        return None
    dispatch_id = row.get('dispatch_id')
    start_idx = None
    if isinstance(dispatch_id, str) and dispatch_id:
        for idx, ev in enumerate(events):
            if dispatch_id in ev.paseo_ids:
                start_idx = idx
                break
    if start_idx is None:
        if row.get('operation') == 'resume':
            launched_epoch, _ = _parse_iso(row.get('launched_at'))
            if launched_epoch is None:
                return None
            for idx, ev in enumerate(events):
                if ev.ts_epoch is not None and ev.ts_epoch >= launched_epoch:
                    start_idx = idx
                    break
            if start_idx is None:
                return None
        else:
            start_idx = 0

    end_idx = len(events)
    if other_dispatch_ids:
        for idx in range(start_idx + 1, len(events)):
            if any(oid in other_dispatch_ids for oid in events[idx].paseo_ids):
                end_idx = idx
                break
    return start_idx, end_idx


def _sum_active_gaps(times: list[float], idle_threshold: float) -> float:
    if len(times) < 2:
        return 0.0
    return round(sum(b - a for a, b in zip(times, times[1:]) if 0.0 <= b - a <= idle_threshold), 3)


def _compute_claude_tokens(events: list[_TranscriptEvent], start_idx: int, end_idx: int) -> tuple[dict | None, str | None]:
    seen_before: set[str] = set()
    for ev in events[:start_idx]:
        if ev.claude_usage is not None:
            seen_before.add(ev.claude_usage[0])

    by_id: dict[str, dict] = {}
    for ev in events[start_idx:end_idx]:
        if ev.claude_usage is None:
            continue
        msg_id, usage = ev.claude_usage
        if msg_id in seen_before:
            continue
        by_id[msg_id] = usage

    if not by_id:
        return None, 'no_token_usage_events_in_slice'

    def _sum_field(key: str) -> tuple[int | None, bool]:
        present = False
        total = 0
        for u in by_id.values():
            if key in u and _is_int_num(u[key]):
                present = True
                total += int(u[key])
        return (total if present else None), present

    inp, inp_ok = _sum_field('input_tokens')
    out, out_ok = _sum_field('output_tokens')
    c_read, cr_ok = _sum_field('cache_read_input_tokens')
    c_write, cw_ok = _sum_field('cache_creation_input_tokens')

    if not inp_ok and not out_ok and not cr_ok and not cw_ok:
        return None, 'no_token_usage_events_in_slice'

    payload: dict[str, int | str | None] = {
        'input_tokens': inp,
        'output_tokens': out,
        'cache_read_tokens': c_read,
        'cache_write_tokens': c_write,
        'raw_input_tokens': inp,
    }
    if not inp_ok:
        payload['input_reason'] = 'input_tokens_absent'
    if not out_ok:
        payload['output_reason'] = 'output_tokens_absent'
    if not cr_ok:
        payload['cache_read_reason'] = 'cache_read_input_tokens_absent'
    if not cw_ok:
        payload['cache_write_reason'] = 'cache_creation_input_tokens_absent'
    return payload, None


def _has_codex_breakdown(usage: dict | None) -> bool:
    return isinstance(usage, dict) and _is_int_num(usage.get('input_tokens')) and _is_int_num(usage.get('output_tokens'))


def _compute_codex_tokens(events: list[_TranscriptEvent], start_idx: int, end_idx: int) -> tuple[dict | None, str | None]:
    baseline_total: dict | None = None
    prev_total_tokens: int | None = None
    for ev in events[:start_idx]:
        info = ev.codex_token_info
        if not isinstance(info, dict):
            continue
        tot = info.get('total_token_usage')
        if isinstance(tot, dict):
            if _has_codex_breakdown(tot):
                baseline_total = tot
            if _is_int_num(tot.get('total_tokens')):
                prev_total_tokens = int(tot['total_tokens'])

    end_total: dict | None = None
    step_counts = {'input_tokens': 0, 'output_tokens': 0, 'cached_input_tokens': 0, 'reasoning_output_tokens': 0}
    step_present = {'input_tokens': False, 'output_tokens': False, 'cached_input_tokens': False, 'reasoning_output_tokens': False}
    step_seen = False
    prev_untracked_step: dict | None = None

    for ev in events[start_idx:end_idx]:
        info = ev.codex_token_info
        if not isinstance(info, dict):
            continue
        tot = info.get('total_token_usage') if isinstance(info.get('total_token_usage'), dict) else None
        step = info.get('last_token_usage') if isinstance(info.get('last_token_usage'), dict) else None

        if _has_codex_breakdown(tot):
            end_total = tot

        tot_num = int(tot['total_tokens']) if isinstance(tot, dict) and _is_int_num(tot.get('total_tokens')) else None
        advance = False
        if step is not None:
            if tot_num is not None:
                if prev_total_tokens is None or tot_num > prev_total_tokens:
                    advance = True
                    prev_total_tokens = tot_num
            elif step != prev_untracked_step:
                advance = True
                prev_untracked_step = step
        elif tot_num is not None:
            if prev_total_tokens is None or tot_num > prev_total_tokens:
                prev_total_tokens = tot_num

        if advance and step is not None:
            for key in step_counts:
                if _is_int_num(step.get(key)):
                    step_counts[key] += int(step[key])
                    step_present[key] = True
                    step_seen = True

    raw_inp: int | None = None
    out_tok: int | None = None
    cached_inp: int | None = None
    reasoning_out: int | None = None

    if end_total is not None and (baseline_total is not None or start_idx == 0 or not step_seen):
        base = baseline_total or {}
        raw_inp = max(0, int(end_total['input_tokens']) - int(base.get('input_tokens') or 0))
        out_tok = max(0, int(end_total['output_tokens']) - int(base.get('output_tokens') or 0))
        if _is_int_num(end_total.get('cached_input_tokens')):
            cached_inp = max(0, int(end_total['cached_input_tokens']) - int(base.get('cached_input_tokens') or 0))
        if _is_int_num(end_total.get('reasoning_output_tokens')):
            reasoning_out = max(0, int(end_total['reasoning_output_tokens']) - int(base.get('reasoning_output_tokens') or 0))
    elif step_seen:
        raw_inp = step_counts['input_tokens'] if step_present['input_tokens'] else None
        out_tok = step_counts['output_tokens'] if step_present['output_tokens'] else None
        cached_inp = step_counts['cached_input_tokens'] if step_present['cached_input_tokens'] else None
        reasoning_out = step_counts['reasoning_output_tokens'] if step_present['reasoning_output_tokens'] else None
    else:
        return None, 'no_token_usage_events_in_slice'

    uncached_inp = max(0, raw_inp - cached_inp) if (raw_inp is not None and cached_inp is not None) else raw_inp
    payload: dict[str, int | str | None] = {
        'input_tokens': uncached_inp,
        'output_tokens': out_tok,
        'cache_read_tokens': cached_inp,
        'cache_write_tokens': None,
        'cache_write_reason': 'codex_does_not_expose_cache_write_tokens',
        'raw_input_tokens': raw_inp,
    }
    if reasoning_out is not None:
        payload['reasoning_output_tokens'] = reasoning_out
    if uncached_inp is None:
        payload['input_reason'] = 'input_tokens_absent'
    if out_tok is None:
        payload['output_reason'] = 'output_tokens_absent'
    if cached_inp is None:
        payload['cache_read_reason'] = 'cached_input_tokens_absent'
    return payload, None


def collect_session_telemetry(
    row: dict,
    *,
    idle_threshold: int,
    agent_doc: dict | None = None,
    other_dispatch_ids: set[str] | None = None,
    home: Path | None = None,
    paseo_home: Path | None = None,
) -> dict:
    """Collect active time, follow-up boundary and token usage from worker session logs."""
    p_home = Path(paseo_home or os.environ.get('PASEO_HOME') or Path.home() / '.paseo')
    harness, provider_env = _resolve_harness(row, agent_doc, p_home)

    persistence = (agent_doc.get('persistence') if isinstance(agent_doc, dict) else None) or {}
    raw_sid = persistence.get('sessionId')
    persisted_sid = raw_sid.strip() if isinstance(raw_sid, str) and raw_sid.strip() else None

    if harness not in ('claude', 'codex'):
        reason = _unsupported_harness_reason(harness)
        return _null_session_telemetry(harness, reason, session_id=persisted_sid, session_reason=reason)

    roots = _candidate_provider_roots(harness, row, provider_env, home, p_home)
    if not roots:
        return _null_session_telemetry(harness, 'provider_home_not_found', session_id=persisted_sid)

    transcript_path, matched_sid = _locate_transcript(
        harness,
        roots,
        persisted_sid,
        row.get('dispatch_id'),
        row.get('launched_at'),
    )
    if transcript_path is None:
        return _null_session_telemetry(harness, 'session_transcript_not_found', session_id=matched_sid)

    try:
        events = _parse_claude_transcript(transcript_path) if harness == 'claude' else _parse_codex_transcript(transcript_path)
    except OSError:
        return _null_session_telemetry(harness, 'session_transcript_unreadable', session_id=matched_sid)

    bounds = _slice_events_for_dispatch(events, row, other_dispatch_ids or set())
    if bounds is None:
        return _null_session_telemetry(harness, 'dispatch_slice_not_found', session_id=matched_sid)
    start_idx, end_idx = bounds
    slice_events = events[start_idx:end_idx]

    token_usage, token_reason = (
        _compute_claude_tokens(events, start_idx, end_idx)
        if harness == 'claude'
        else _compute_codex_tokens(events, start_idx, end_idx)
    )

    indexed_timed = [(ev.ts_epoch, idx, ev) for idx, ev in enumerate(slice_events) if ev.ts_epoch is not None]
    indexed_timed.sort(key=lambda item: (item[0], item[1]))
    timed_events = [item[2] for item in indexed_timed]
    all_times = [item[0] for item in indexed_timed]

    prompt_positions = [idx for idx, ev in enumerate(timed_events) if ev.is_user_prompt]
    untimed_prompts = sum(1 for ev in slice_events if ev.is_user_prompt and ev.ts_epoch is None)
    total_user_turns = len(prompt_positions) + untimed_prompts
    followup_turns = max(0, total_user_turns - 1)

    if len(all_times) < 2:
        return {
            'telemetry_harness': harness,
            'worker_session_id': matched_sid,
            'worker_session_reason': None if matched_sid else 'session_id_unavailable',
            'active_seconds': None,
            'idle_threshold_seconds': None,
            'active_seconds_reason': 'insufficient_timestamped_events',
            'token_usage': token_usage,
            'token_usage_reason': token_reason,
            'initial_active_seconds': None,
            'followup_active_seconds': None,
            'first_followup_at': timed_events[prompt_positions[1]].ts_iso if len(prompt_positions) >= 2 else None,
            'user_turns': total_user_turns,
            'followup_turns': followup_turns,
            'followup_boundary_reason': 'insufficient_timestamped_events',
        }

    threshold = float(idle_threshold)
    active_secs = _sum_active_gaps(all_times, threshold)

    if len(prompt_positions) >= 2:
        split_at = prompt_positions[1]
        first_followup_at = timed_events[split_at].ts_iso
        before_times = all_times[:split_at]
        after_times = all_times[split_at:]
        initial_active = _sum_active_gaps(before_times, threshold)
        followup_active = _sum_active_gaps(after_times, threshold)
        boundary_reason = None
    elif total_user_turns == 1 and len(prompt_positions) == 1:
        first_followup_at = None
        initial_active = active_secs
        followup_active = 0.0
        boundary_reason = 'no_parent_followup'
    else:
        first_followup_at = None
        initial_active = None
        followup_active = None
        boundary_reason = 'no_user_prompt_events_in_slice' if total_user_turns == 0 else 'untimed_followup_prompt'

    return {
        'telemetry_harness': harness,
        'worker_session_id': matched_sid,
        'worker_session_reason': None if matched_sid else 'session_id_unavailable',
        'active_seconds': active_secs,
        'idle_threshold_seconds': int(idle_threshold),
        'active_seconds_reason': None,
        'token_usage': token_usage,
        'token_usage_reason': token_reason,
        'initial_active_seconds': initial_active,
        'followup_active_seconds': followup_active,
        'first_followup_at': first_followup_at,
        'user_turns': total_user_turns,
        'followup_turns': followup_turns,
        'followup_boundary_reason': boundary_reason,
    }


def collect_ground_truth(
    row: dict,
    *,
    idle_threshold: int,
    recorded_at: str,
    agent_doc: dict | None = None,
    other_dispatch_ids: set[str] | None = None,
    home: Path | None = None,
    paseo_home: Path | None = None,
) -> dict:
    """Assemble full completion ground-truth fields for a dispatch row."""
    session_fields = collect_session_telemetry(
        row,
        idle_threshold=idle_threshold,
        agent_doc=agent_doc,
        other_dispatch_ids=other_dispatch_ids,
        home=home,
        paseo_home=paseo_home,
    )
    git_fields = collect_git_changes(row)
    return {
        'ground_truth_recorded_at': recorded_at,
        **session_fields,
        **git_fields,
    }
