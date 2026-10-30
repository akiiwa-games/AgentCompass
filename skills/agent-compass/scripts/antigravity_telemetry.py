"""Read native Antigravity telemetry; never return transcript content or plugin payloads."""

import hashlib
import json
import math
from pathlib import Path
import re


def safe_key(value):
    value = value.strip()
    if re.fullmatch(r'[A-Za-z0-9._-]{1,120}', value) and '..' not in value:
        return value
    prefix = re.sub(r'[^A-Za-z0-9_-]', '_', value)[:32] or 'conv'
    return prefix + '-' + hashlib.sha256(value.encode()).hexdigest()[:32]


def transcript(persistence, home):
    """Resolve Paseo's plugin:<JSON> handle or structured ProviderPersistence."""
    provider = persistence.get('provider')
    if provider not in ('antigravity', 'antigravity-resilient'):
        return None
    session = persistence.get('sessionId')
    payload = persistence
    if isinstance(session, str) and session.startswith('plugin:'):
        try:
            payload = json.loads(session[len('plugin:'):])
        except ValueError:
            return None
    data = payload.get('data') if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    conversation = data.get('conversationId')
    if not conversation and isinstance(data.get('sessionKey'), str):
        binding = Path(home) / '.paseo/antigravity-resilient/conversations/session-keys' / (safe_key(data['sessionKey']) + '.json')
        try:
            conversation = json.loads(binding.read_text(encoding='utf-8')).get('conversationId')
        except (OSError, ValueError, AttributeError):
            return None
    if not conversation and isinstance(session, str) and not session.startswith('plugin:'):
        conversation = session
    if not isinstance(conversation, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', conversation):
        return None
    path = Path(home) / '.gemini/antigravity-cli/brain' / conversation / '.system_generated/logs/transcript.jsonl'
    return (provider, path) if path.is_file() else None


def state(path):
    """Count native CHECKPOINTs once, using the last input+cache reading before each.

    Replayed UI notifications are deliberately not input. Stable native step indices
    deduplicate repeated checkpoints even when their export timestamp changes.
    Native transcript PLANNER_RESPONSE rows have flat input_tokens/cache_read_tokens;
    nested usage is accepted for older adapters. Output tokens are excluded because
    this metric measures the model's input context, not total or billed token usage.
    """
    compactions, compacted, context = 0, 0, None
    seen = set()
    partial = False
    checkpoint_pending = False
    def tokens(value):
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)
    try:
        with open(path, encoding='utf-8') as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    step = json.loads(line)
                except ValueError:
                    partial = True
                    continue
                if not isinstance(step, dict):
                    partial = True
                    continue
                kind = step.get('type')
                if kind not in ('CHECKPOINT', 'PLANNER_RESPONSE'):
                    continue
                index = step.get('step_index')
                identity = (kind, index) if isinstance(index, int) else (kind, step.get('id') or step.get('created_at'))
                if identity[1] is not None:
                    if identity in seen:
                        continue
                    seen.add(identity)
                else:
                    partial = True
                if kind == 'CHECKPOINT':
                    compactions += 1
                    if context is None and not checkpoint_pending:
                        partial = True
                    compacted += context or 0
                    context = None
                    checkpoint_pending = True
                else:
                    usage = step if 'input_tokens' in step or 'cache_read_tokens' in step else step.get('usage')
                    if not isinstance(usage, dict):
                        partial = True
                        context = None
                        continue
                    incoming, cached = usage.get('input_tokens'), usage.get('cache_read_tokens', 0)
                    if tokens(incoming) and tokens(cached):
                        context = incoming + cached
                        checkpoint_pending = False
                    else:
                        partial = True
                        context = None
    except (OSError, UnicodeError):
        return {'telemetry_status': 'unknown', 'telemetry_reason': 'native_transcript_unreadable'}
    return {'telemetry_status': 'partial' if partial or (context is None and not checkpoint_pending) else 'observed',
            'telemetry_source': 'antigravity_native_transcript',
            'compactions': compactions, 'compacted_tokens': compacted,
            'historical_context_tokens': compacted + (context or 0),
            **({'context_tokens': context} if context is not None else {})}
