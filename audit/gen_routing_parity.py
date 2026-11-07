"""Generate the routing data the other repositories compare against, from one fixed commit of AgentCompass.

    python -X utf8 audit/gen_routing_parity.py --out parity.json

The router and its data are read with `git archive <sha>`, never from the working tree or a moving ref, so the output is
the same on every machine. PINNED_SHA moves only by an edit here or AGENTCOMPASS_PARITY_SHA / --sha for one run.
Fields `source`, `models`, `variants`, `canonical`, `effort_curves`, `dispatch_efforts`, `responsiveness`,
`stepdown_columns` and `stepdown` match Magpie's test/fixtures/gen_realtime_parity.py.
"""
import argparse
import copy
from datetime import datetime, timedelta, timezone
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

PINNED_SHA = '7f83f141783bee70002e08191f16cde09276a5df'
REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = 'skills/agent-compass/scripts'
NOW = datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)

EFFORTS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra', 'default']
GATEWAY = 'openrouter'      # a provider with no dispatch table
# Spellings by what the resolver must do with them; every other repository resolves the same list.
VARIANTS = [
    'claude-opus-5-5-20261001', 'claude-opus-5-5@20261001', 'claude-sonnet-4-6-20250514', 'gpt-6-astra-20261001',
    'gpt-6.1-sol-latest', 'gpt-6-astra-preview', 'grok-4.7-latest', 'CLAUDE-OPUS-5-5', 'CLAUDE-SONNET-4-6',
    'Anthropic/Claude-Opus-4-6-20250514', 'OpenAI/GPT-6-Astra-Latest', 'claude-sonnet-4-6[1m]', 'claude-opus-5-5[1m]',
    'claude-opus-5', 'claude-sonnet-5', 'qwen3-flash-2025-07-28',
    'XAI/Grok-4.7:HIGH', 'claude-opus-5-5:medium', 'gpt-6-astra-medium', 'openrouter/anthropic/claude-opus-4-6:max',
    'qwen/Qwen3-Coder-Next:free',
    'opencode/deepseek-v4-flash', 'OpenCode/DeepSeek-V4-Flash',
    'claude-opus-4-20250514', 'claude-sonnet-4-20250514', 'gpt-6-experimental', 'gemini-4-pro-exp', 'gemini-2.5-pro',
    'deepseek-r1', 'deepseek-v3', 'grok-3', 'o3', 'gpt-5', 'claude-3-opus', 'llama-4-maverick', 'kimi-k4-preview',
    'totally-unknown-model',
]
PROFILE = {'reasoning_depth': 2, 'code_complexity': 1, 'context_scale': 1, 'hallucination_risk': 1, 'error_cost': 1,
           'needs_visual': False, 'needs_gui': False, 'local_only': True, 'interaction_style': 'engineering',
           'multi_task': False, 'task_count': '1', 'reason': 'parity', 'work_kind': 'general_implementation'}
TASK_COUNTS = [[False, 1], [True, 2], [True, 3], [True, 4], [True, 9], [True, 2.0], [False, '1'], [True, '>3'],
               [True, 1], [True, '1'], [False, 2], [False, '>3'], [True, 0], [True, -1], [True, 1.5], [True, 'nan'],
               [True, 'inf'], [True, 'many'], [True, None], [True, '4'], [True, True], ['yes', 2]]
PINS = [
    {'constraints': {'model': 'claude-opus-5-5', 'effort': 'medium'}},
    {'constraints': {'model': 'claude-opus-5-5:medium'}},
    {'pinned_model': 'claude-opus-5-5', 'pinned_effort': 'medium'},
    {'constraints': {'model': 'gpt-6-astra', 'effort': 'medium'}},
    {'constraints': {'model': 'claude-haiku-5-5', 'effort': 'medium'}},
    {'constraints': {'model': 'gemini-2.5-pro'}},
    {'constraints': {'model': 'gpt-6-astra'}},
    {'constraints': {'model': 'gpt-6-astra', 'effort': 'xhigh'}},
    {'constraints': {'model': 'claude-opus-5-5'}},
]
QUOTA = {
    'claude': [('five_hour', 90, 3), ('seven_day', 80, 100), ('weekly_fable', 100, 100)],
    'claude-lab': [('five_hour', 90, 3), ('seven_day', 80, 100), ('weekly_fable', 100, 100)],
    'codex': [('usage', 80, 100)],
    'antigravity': [('gemini-5h', 90, 3), ('gemini-weekly', 80, 100), ('3p-5h', 90, 3), ('3p-weekly', 80, 100)],
}


def pinned_sha(environ=None):
    return (os.environ if environ is None else environ).get('AGENTCOMPASS_PARITY_SHA') or PINNED_SHA


def extract(sha, destination, repository=REPOSITORY):
    """The commit's own router, data and fixtures; `sha` may not be a ref name, so a moving branch cannot be pinned."""
    full = subprocess.run(['git', '-C', str(repository), 'rev-parse', '--verify', f'{sha}^{{commit}}'],
                          capture_output=True, text=True, check=True).stdout.strip()
    if not full.startswith(sha):
        raise ValueError(f'{sha!r} is a ref name, not a commit id; pin a commit so the data cannot move')
    archive = subprocess.run(['git', '-C', str(repository), 'archive', '--format=tar', full, 'skills/agent-compass'],
                             capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(destination, filter='data')
    return full


def plain(value):
    """JSON has no NaN or Infinity, so the table spells them; validation gets the real float."""
    return {'nan': float('nan'), 'inf': float('inf')}.get(value, value) if isinstance(value, str) else value


def generate(sha, repository=REPOSITORY):
    with tempfile.TemporaryDirectory(prefix='routing-parity-', ignore_cleanup_errors=True) as directory:
        full = extract(sha, directory, repository)
        scripts = Path(directory) / SCRIPTS
        # A caller may already hold the working tree's modules under the same names; put them back afterwards.
        own = {path.stem for path in scripts.glob('*.py')}
        held = {name: sys.modules.pop(name) for name in own if name in sys.modules}
        sys.path.insert(0, str(scripts))
        importlib.invalidate_caches()
        cleanups = []
        try:
            support = importlib.import_module('router_test_support')
            support.isolated_router_state(cleanups.append)
            allocator = importlib.import_module('allocator')
            auto_bundles = importlib.import_module('auto_bundles')
            if not str(Path(allocator.__file__).resolve()).startswith(str(scripts.resolve())):
                raise RuntimeError(f'allocator came from {allocator.__file__}, not the extracted commit')
            return build(allocator, auto_bundles, support, full, Path(directory))
        finally:
            for cleanup in reversed(cleanups):
                cleanup()
            sys.path.remove(str(scripts))
            for name in own:
                sys.modules.pop(name, None)
            sys.modules.update(held)


def build(allocator, auto_bundles, support, commit, root):
    document = json.loads((root / 'skills/agent-compass/references/accounts.json').read_text(encoding='utf-8-sig'))
    policy = document['scoring']
    dispatch = document.get('dispatch_efforts') or {}
    empty = root / 'no-telemetry'
    empty.mkdir()
    models = [k for k, v in document['models'].items() if isinstance(v, dict)]
    names = models + [v for v in VARIANTS if v not in models]

    def own_provider(key):
        return next((p for p, table in dispatch.items() if key in table), None)

    def step(model, effort, provider, gate):
        key = allocator.canonical(model, document)
        bundle = {'model': model, 'effort': effort, 'provider': provider, 'account': provider}
        ctx = {'accounts': document, 'policy': policy, 'bundles': {}, 'state_dir': empty}
        return [model, effort, provider, gate, *allocator.resolve_realtime_effort(
            bundle, {'realtime_loop': True}, ctx, key, allocator.model_spec(document, model), policy,
            allocator.model_curves(document, model), gate)]

    rows = []
    for name in names:
        key = allocator.canonical(name, document)
        providers = [GATEWAY] + ([own_provider(key)] if own_provider(key) else [])
        for effort in EFFORTS:
            for provider in providers:
                for gate in ([0.0, 3.0, 4.2] if name in models else [3.0]):
                    rows.append(step(name, effort, provider, gate))

    def verdict(multi, count):
        try:
            profile = allocator.validate_profile({**PROFILE, 'multi_task': multi, 'task_count': plain(count)})
        except ValueError:
            return {'multi_task': multi, 'task_count': count, 'valid': False}
        return {'multi_task': multi, 'task_count': count, 'valid': True, 'bucket': profile['task_count']}

    fixture = allocator.load_accounts(root / SCRIPTS / 'fixtures/reference-accounts.json')
    usage = support.usage_windows(NOW, QUOTA)

    def planned(extra, realtime):
        profile = {**PROFILE, **({'realtime_loop': True} if realtime else {})}
        task = {'objective': 'parity', 'profile': profile, 'expected_hours': 1, 'disable_fast': True,
                'include_unverified': True, **copy.deepcopy(extra)}
        plan = allocator.plan(task, copy.deepcopy(usage), NOW, accounts=copy.deepcopy(fixture), overrides=[])
        selected = plan.get('selected_bundle') or {}
        decision = plan.get('decision') or {}
        return {'task': extra, 'realtime_loop': realtime, 'choice': plan.get('choice'), 'model': selected.get('model'),
                'effort': selected.get('effort'), 'blocked': decision.get('blocked'),
                'warnings': decision.get('warnings') or []}

    return {
        'source': {'repo': 'AgentCompass', 'commit': commit[:7], 'full_commit': commit,
                   'file': 'skills/agent-compass/references/accounts.json', 'generated_for': NOW.isoformat()},
        'models': models,
        'variants': VARIANTS,
        'canonical': {name: allocator.canonical(name, document) for name in names},
        'resolutions': {name: {k: allocator.resolve_model(name, document)[k] for k in ('key', 'source', 'effort')} for name in names},
        'family_shrink': {'tier': auto_bundles.FAMILY_TIER_SHRINK, 'vector': auto_bundles.FAMILY_VECTOR_SHRINK},
        'effort_curves': {k: v for k, v in document['effort_curves'].items() if isinstance(v, dict)},
        'dispatch_efforts': dispatch,
        'responsiveness': {name: {eff: list(allocator.bundle_responsiveness(
            {'model': name, 'effort': eff}, allocator.model_spec(document, name), policy, allocator.model_curves(document, name), state_dir=empty))
            for eff in EFFORTS} for name in names},
        'stepdown_columns': ['model', 'effort', 'provider', 'gate', 'effective', 'value', 'source', 'stepped_from'],
        'stepdown': rows,
        'profile_validation': [verdict(multi, count) for multi, count in TASK_COUNTS],
        'pins': [planned(extra, realtime) for extra in PINS for realtime in (False, True)],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--sha', default=None, help='commit to read instead of PINNED_SHA')
    args = parser.parse_args(argv)
    result = generate(args.sha or pinned_sha())
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(f"{result['source']['commit']}: {len(result['stepdown'])} step-down rows -> {args.out}")


if __name__ == '__main__':
    main()
