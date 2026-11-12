"""Worker session reads: the Paseo agent record, its harness session log, and the context it has used."""

import json
from pathlib import Path

import antigravity_telemetry
import provider_inventory


_AGENT_RECORD_CACHE = {}
_SESSION_LOG_CACHE = {}
_SESSION_STATE_CACHE = {}


def session_log(agent_id, home=None):
    """(provider, log path) of a Paseo agent's harness session, or None. Paseo keeps the harness session id in
    ~/.paseo/agents/<workspace>/<agent id>.json; Codex, Claude and Pi keep one log per session."""
    explicit_home = Path(home) if home else None
    home = explicit_home or Path.home()
    agents_root = home / '.paseo' / 'agents' if (home / '.paseo' / 'agents').is_dir() else home / 'agents'
    if not isinstance(agent_id, str) or any(c in agent_id for c in '*?[]/\\') or agent_id in ('.', '..'):
        return None
    rec_key = (str(agents_root), agent_id)
    record = _AGENT_RECORD_CACHE.get(rec_key)
    if record is not None and not record.is_file():
        record = None
    try:
        if record is None:
            record = next(agents_root.glob(f'*/{agent_id}.json'), None)
            if record is not None:
                _AGENT_RECORD_CACHE[rec_key] = record
        persistence = json.loads(record.read_text(encoding='utf-8')).get('persistence') or {} if record else {}
    except (OSError, ValueError):
        return None
    if not isinstance(persistence, dict):
        return None
    if persistence.get('provider') in ('antigravity', 'antigravity-resilient'):
        return antigravity_telemetry.transcript(persistence, home)
    provider, session = persistence.get('provider'), persistence.get('sessionId')
    if not provider or not session or any(c in str(session) for c in '*?[]/\\'):
        return None
    log_key = (str(home), provider, str(session))
    cached_log = _SESSION_LOG_CACHE.get(log_key)
    if cached_log is not None and cached_log[1].is_file():
        return cached_log
    if (explicit_home is None
            and not hasattr(provider_inventory.discover, '_mock_name')
            and not hasattr(provider_inventory.current, '_mock_name')):
        inventory = provider_inventory.current()
    else:
        inventory = provider_inventory.discover(home=explicit_home)
    row = (inventory.get('accounts') or {}).get(provider) or {}
    family = row.get('family') or provider_inventory.launcher_family(provider)
    root = Path(row['home']) if row.get('home') else home / ('.pi/agent' if family == 'pi' else '.' + str(family))
    patterns = {'codex': [f'sessions/**/rollout-*-{session}.jsonl', f'archived_sessions/rollout-*-{session}.jsonl'],
                'claude': [f'projects/*/{session}.jsonl'],
                'pi': [f'sessions/**/*{session}*.jsonl']}
    for pattern in patterns.get(family, []):
        found = next(root.glob(pattern), None)
        if found:
            _SESSION_LOG_CACHE[log_key] = (family, found)
            return family, found
    return None


def session_state(agent_id, home=None):
    """{'compactions', 'compacted_tokens', 'context_tokens', 'historical_context_tokens'} from the agent's session log.
    `compacted_tokens` sums the historical pre-compaction context each compaction replaced (Codex: the last positive reading
    before the event; Claude `preTokens` / `cumulativeDroppedTokens`; Pi `tokensBefore`); `context_tokens` is the latest
    active reading; `historical_context_tokens` is their uncompressed sum (`compacted_tokens + context_tokens`)."""
    located = session_log(agent_id, home)
    if not located:
        return {'telemetry_status': 'unknown', 'telemetry_reason': 'session_log_unavailable'}
    provider, path = located
    if provider in ('antigravity', 'antigravity-resilient'):
        return antigravity_telemetry.state(path)
    state_key = None
    try:
        st = path.stat()
        state_key = (provider, str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        state_key = None
    if state_key is not None and state_key in _SESSION_STATE_CACHE:
        return dict(_SESSION_STATE_CACHE[state_key])
    compactions, compacted, context = 0, 0, None
    try:
        with open(path, encoding='utf-8', errors='ignore') as handle:
            for line in handle:
                if provider == 'codex':
                    if '"type":"compacted"' in line:
                        compactions, compacted, context = compactions + 1, compacted + (context or 0), None
                    elif '"token_count"' in line and '"last_token_usage"' in line:
                        usage = ((json.loads(line).get('payload') or {}).get('info') or {}).get('last_token_usage') or {}
                        inp = usage.get('input_tokens')
                        if isinstance(inp, (int, float)) and not isinstance(inp, bool) and inp > 0:
                            context = int(inp)
                elif provider == 'claude':
                    if '"compact_boundary"' in line:
                        meta = json.loads(line).get('compactMetadata') or {}
                        before = meta.get('preTokens')
                        dropped = meta.get('cumulativeDroppedTokens')
                        post = meta.get('postTokens')
                        compactions += 1
                        compacted = max(compacted + (before or context or 0), dropped if isinstance(dropped, int) else 0)
                        context = post if isinstance(post, int) and post > 0 else None
                    elif '"assistant"' in line and '"usage"' in line:
                        usage = (json.loads(line).get('message') or {}).get('usage') or {}
                        cur = sum(usage.get(k) or 0 for k in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
                        if cur > 0:
                            context = cur
                elif '"type":"compaction"' in line:
                    before = json.loads(line).get('tokensBefore')
                    compactions, compacted, context = compactions + 1, compacted + (before or context or 0), None
                elif '"usage"' in line and '"assistant"' in line:
                    usage = (json.loads(line).get('message') or {}).get('usage') or {}
                    context = sum(usage.get(k) or 0 for k in ('input', 'cacheRead', 'cacheWrite')) or context
    except (OSError, ValueError):
        return {}
    result = {'compactions': compactions, 'compacted_tokens': compacted,
              'historical_context_tokens': compacted + (context or 0),
              **({'context_tokens': context} if context is not None else {})}
    if state_key is not None:
        _SESSION_STATE_CACHE[state_key] = dict(result)
    return result


def worker_historical_tokens(worker):
    """Total historical uncompressed context tokens (sum of pre-compaction contexts + current active context),
    never just the post-compaction compressed window."""
    if not isinstance(worker, dict):
        return 0
    compacted = worker.get('compacted_tokens') or 0
    current = worker.get('context_tokens') or 0
    hist = worker.get('historical_context_tokens') or 0
    return max(hist, compacted + current)


SHIPPED_ACCOUNTS = Path(__file__).resolve().parents[1] / 'references' / 'accounts.json'


def shipped_scoring(path=None):
    """`scoring` mapping from the shipped `accounts.json`."""
    source = Path(path) if path is not None else SHIPPED_ACCOUNTS
    doc = json.loads(source.read_text(encoding='utf-8-sig'))
    scoring = doc.get('scoring') if isinstance(doc, dict) else None
    if not isinstance(scoring, dict):
        raise KeyError(f'Missing scoring block in {source}')
    return scoring


def rotation_threshold(policy=None):
    """Configured historical-context rotation limit (`scoring.rotate_after_carried_tokens`)."""
    if policy is None:
        import account_fleet
        policy = account_fleet.load_accounts().get('scoring')
    elif isinstance(policy, dict) and 'scoring' in policy and 'rotate_after_carried_tokens' not in policy:
        policy = policy.get('scoring')
    if not isinstance(policy, dict) or policy.get('rotate_after_carried_tokens') is None:
        raise KeyError('Missing scoring.rotate_after_carried_tokens in accounts configuration')
    raw = policy['rotate_after_carried_tokens']
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        raise ValueError(f'scoring.rotate_after_carried_tokens must be a positive number, got {raw!r}')
    return int(raw)


def worker_worn(worker, policy):
    """True when the worker has reached `rotate_after_carried_tokens`
    measured by historical uncompressed context tokens (never compressed window size)."""
    limit = rotation_threshold(policy)
    if not isinstance(worker, dict):
        return False
    return worker_historical_tokens(worker) >= limit
