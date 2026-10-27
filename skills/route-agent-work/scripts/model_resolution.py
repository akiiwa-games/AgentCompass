"""Local model-name normalization shared by discovery and allocation.

Defaults are routing priors, not measured benchmarks or provider price quotes.
Always preserve an exact calibrated model/alias before trying family fallbacks.
"""
import copy
import re


GENERIC_MODEL_SPEC = {
    'tier': 3.3, 'cost': 2, 'visual': False, 'usd_per_hour': 1.5,
    'capabilities': {'reasoning': 0.55, 'coding': 0.55, 'exploration': 0.55,
                     'anti_hallucination': 0.5, 'visual': 0.0, 'gui': 0.0},
    'subjective': {'divergence': 0.5, 'global_rigor': 0.5, 'module_precision': 0.5},
    'basis': 'Conservative provisional baseline for an unrecognized advertised model.',
}


def generic_model_spec():
    return copy.deepcopy(GENERIC_MODEL_SPEC)


def resolve_canonical_model(raw_model_name, known_models, aliases=None):
    """Return a known canonical ID, or None for the generic baseline.

    Unknown launch IDs remain untouched by callers: matching affects scores only.
    """
    aliases = aliases or {}
    known = {k for k, v in known_models.items() if isinstance(v, dict)}

    def exact(name):
        seen = set()
        while name in aliases and name not in seen:
            seen.add(name)
            name = aliases[name]
        return name if name in known else None

    raw = str(raw_model_name or '').strip()
    for name in (raw, raw.rsplit('/', 1)[-1]):
        if match := exact(name):
            return match
    name = raw.rsplit('/', 1)[-1].lower()
    while True:
        stripped = re.sub(r'(?:@\d{8}|-\d{8}|:\w+|-(?:latest|preview))$', '', name)
        if stripped == name:
            break
        name = stripped
        if match := exact(name):
            return match
    for prefix in sorted(known, key=lambda x: (-len(x), x)):
        # A delimiter prevents e.g. gpt-6.10 from accidentally matching gpt-6.1.
        if name.startswith(prefix + '-') or name.startswith(prefix + ':'):
            if match := exact(prefix):
                return match
    families = (
        (r'^claude-opus', 'claude-opus-5-5'),
        (r'^claude-sonnet', 'claude-sonnet-5-5'),
        (r'^claude-fable', 'claude-fable-5-1'),
        (r'^claude-haiku', 'claude-haiku-5-5'),
        (r'^gpt-6.*terra', 'gpt-6.1-terra'),
        (r'^gpt-6.*sol', 'gpt-6.1-sol'), (r'^gpt-6.*luna', 'gpt-6-luna'),
        (r'^gpt-6.*pro', 'gpt-6-pro'), (r'^gpt-6', 'gpt-6-astra'),
        (r'^gemini.*flash', 'gemini-3.8-flash-high'),
        (r'^gemini', 'gemini-4-argon-high'),
        (r'^grok.*(?:mini|fast)|^grok-code', 'grok-4.7-build-fast'),
        (r'^grok', 'grok-4.7'),
        (r'^deepseek.*flash', 'deepseek-v4.1-flash-free'),
        (r'^deepseek', 'deepseek-v4-pro'),
        (r'^qwen.*coder', 'qwen3-coder-next'), (r'^qwen', 'qwen3.8-max'),
        (r'^kimi|^moonshot', 'kimi-k3.1-pro'),
        (r'^glm.*flash', 'glm-5.3-flash-free'), (r'^glm', 'glm-5.3'),
        (r'^minimax.*flash', 'minimax-m3.1-flash'),
        (r'^minimax|^abab', 'minimax-m3'),
        (r'^doubao', 'doubao-seed-2-1-pro-260628'),
        (r'^mimo.*flash', 'mimo-v2.6-flash-free'), (r'^mimo', 'mimo-v2.6-pro'),
        (r'^nemotron.*lightning', 'nemotron-3.5-lightning-free'),
        (r'^nemotron.*(?:super|vl)', 'nemotron-3.5-super-vl-free'),
        (r'^nemotron', 'nemotron-3-ultra-free'),
        (r'^muse', 'muse-wonder-1.0'),
        # No new Llama ID in the October catalog. Use the generic baseline,
        # rather than resurrecting Llama 4 or pretending Muse is a Llama alias.
        (r'^llama', None),
        (r'^mistral|^codestral|^devstral', 'mistral-large-4'),
    )
    return next((target for pattern, target in families if target in known and re.search(pattern, name)), None)
