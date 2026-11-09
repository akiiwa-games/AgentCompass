"""Community real-usage measurements of subscription window capacity (round 047, round 4).

The user's rule (2026-11-09): window capacity comes from what the community measures on real agentic traffic (cache
included, priced at official API rates), each value cited (source, URL, data date, fetch time, kind `community`), kept
current, never from this machine's own samples. `sources.py fetch` downloads the pages below with the rest; this module
reads the numbers out of them for `measurements.py`.

Every reading names the sentence it is read from. A page that was not fetched, or that no longer carries the sentence,
yields an error and no value: an old copy is never kept (`sources.fetch` deletes a file that failed to download).
Projected or modeled figures are not read (inventivehq's Plus estimate is a projection; ai-cost-estimator style tables
are modeled), only measured ones.

Derived values say how they were derived:
- a five-hour window is the page's own weekly value over the page's own count of full five-hour windows per week;
- a published range is read as its midpoint;
- `rules` carry ratios between windows that a source states (Fable 5.1 can use half of the Claude weekly limit,
  SemiAnalysis 2026-10-05 as quoted by wokey), applied by `five_axis.capacity` to the merged base window.

`mix` is the community real-traffic token split wokey values every plan at (user decision 2026-11-09, option B): the
stated cache-read share of the fixed split (about 92%), and the output share of the non-cache-read tokens from the
Max 20x row measured at that split (its non-cache-read and output tokens per week). `measurements.py` reprices AA's
measured output throughput at this split (`derived.community_mix`), so usd per worker-hour sits on the scale the
capacities are valued on.

Antigravity: Google AI Pro from the eneasf telemetry findings (one harness, September 2026; dollars at Google API rates
with about 93% cache reads, the same scale as wokey's split). Not found (no community measurement published as of
2026-11-09; listed, not invented): Claude Max 5x (wokey is still collecting), Antigravity Ultra 5x / 20x, Pi (spends a
Codex allowance of an unknown plan).
"""
import re
from datetime import datetime

import sources

PAGES = sources.COMMUNITY_PAGES

ISO = (r'Data updated (\d{4}-\d{2}-\d{2})', '%Y-%m-%d')
LONG = (r'Updated ([A-Z][a-z]+ \d{1,2}, \d{4})', '%B %d, %Y')


def num(text):
    text = text.replace(',', '')
    return float(text[:-1]) * 1000 if text.endswith('k') else float(text)


def weekly(groups):
    return num(groups[0])


def per_window(groups):
    """Weekly value over full five-hour windows per week, both from the same sentence."""
    return num(groups[0]) / float(groups[1])


def midpoint(groups):
    return (num(groups[0]) + num(groups[1])) / 2


def both_tracks(index):
    """One figure of a two-column diagram that states the same capacity for both quota tracks; differing columns
    cannot be told apart, so they give no value."""
    def value(groups):
        pairs = (groups[0], groups[1]) if index < 2 else (groups[2], groups[3])
        if num(pairs[0]) != num(pairs[1]):
            raise ValueError(f'the two tracks differ ({pairs[0]} vs {pairs[1]}); which is which is not stated')
        return num(pairs[0])
    return value


DIAGRAM = (r'Capacity: \$([\d.]+) / week\s*│\s*│\s*Capacity: \$([\d.]+) / week\s*│\s*│\s*Burst: \$([\d.]+) / 5h\s*│\s*│\s*'
           r'Burst: \$([\d.]+) / 5h')


# source id -> page, data-date pattern, readings (family, plan, window, sentence pattern, value function, method)
SOURCES = {
    'community.wokey.claude': {
        'file': 'community-wokey-claude.html', 'date': ISO,
        'readings': [
            ('claude', 'pro', 'seven_day', r'Claude Pro \(\$20/mo\) ?: ≈ \$([\d,]+) of API use a week', weekly,
             'median measured account week at API rates (cache included)'),
            ('claude', 'pro', 'five_hour',
             r'Claude Pro \(\$20/mo\) ?: ≈ \$([\d,]+) of API use a week[^·]*·[^·]*· ≈ ([\d.]+) full 5-hour windows', per_window,
             'weekly value / full five-hour windows per week (both from the page)'),
            ('claude', 'max20', 'seven_day', r'Claude Max 20x \(\$200/mo\) ?: ≈ \$([\d,]+) of API use a week', weekly,
             'median measured account week at API rates (cache included)'),
            ('claude', 'max20', 'five_hour',
             r'Claude Max 20x \(\$200/mo\) ?: ≈ \$([\d,]+) of API use a week[^·]*·[^·]*· ≈ ([\d.]+) full 5-hour windows',
             per_window, 'weekly value / full five-hour windows per week (both from the page)'),
        ],
        'mix': {'split': r'value every plan at one fixed traffic split: Claude Code traffic on Wokey in the week to '
                         r'(\d{4}-\d{2}-\d{2}), about (\d+)% of tokens cache reads',
                'row': r'Claude Max 20x \$200/mo \$[\d,]+ \(\$[\d,]+–\$[\d,]+\), ≈ \$[\d,]+/mo \d+× ([\d.]+)M ([\d.]+)M',
                'row_share': r'Claude Max 20x: (\d+)% cache reads'},
        'rules': [
            {'family': 'claude', 'window': 'weekly_fable', 'of_window': 'seven_day', 'ratio': 0.5,
             'pattern': r'Fable 5\.1 can use only half the weekly limit',
             'statement': 'Fable 5.1 can use only half the weekly limit',
             'original': 'https://newsletter.semianalysis.com/p/anthropic-subscriptions-offer-5x (SemiAnalysis, 2026-10-05)'},
        ],
    },
    'community.wokey.codex': {
        'file': 'community-wokey-codex.html', 'date': ISO,
        'readings': [
            ('codex', 'plus', 'usage', r'ChatGPT Plus \(Codex\) \(\$20/mo\) ?: ≈ \$([\d,]+) of API use a week', weekly,
             'median measured account week at API rates (cache included)'),
            ('codex', 'plus', 'five_hour',
             r'ChatGPT Plus \(Codex\) \(\$20/mo\) ?: ≈ \$([\d,]+) of API use a week[^·]*·[^·]*· ≈ ([\d.]+) full 5-hour windows',
             per_window, 'weekly value / full five-hour windows per week (both from the page)'),
            ('codex', 'pro', 'usage', r'ChatGPT Pro \$200 \(Codex\) \(\$200/mo\) ?: ≈ \$([\d,]+) of API use a week', weekly,
             'median measured account week at API rates (cache included)'),
        ],
    },
    'community.finops.codex_plus': {
        'file': 'community-finops-codex-plus.html', 'date': LONG,
        'readings': [
            ('codex', 'plus', 'usage', r'ChatGPT Plus has been worth a median \$(\d+) of API-priced usage', weekly,
             'median of measured full weekly windows at API prices'),
        ],
    },
    'community.finops.codex_pro100': {
        'file': 'community-finops-codex-pro100.html', 'date': LONG,
        'readings': [
            ('codex', 'pro100', 'usage', r'A full Pro \$100 week priced at a median \$(\d+)', weekly,
             'median of a heavy user\'s logged full weekly windows at API prices'),
        ],
    },
    'community.finops.claude_max20': {
        'file': 'community-finops-claude-max20.html', 'date': LONG,
        'readings': [
            ('claude', 'max20', 'seven_day', r'About \$([\d.]+k) of API-equivalent usage at today', weekly,
             'one heavy user\'s measured full week at API-equivalent prices'),
        ],
    },
    'community.inventive.codex': {
        'file': 'community-inventive-codex.html', 'date': (r'By [A-Z][a-z]+ · (\d{4}-\d{2}-\d{2})', '%Y-%m-%d'),
        'readings': [
            ('codex', 'pro', 'usage', r'Pro \$200 \(new 10× tier\): about \$(\d+) of API-priced usage a week', weekly,
             'measured weekly window at API prices'),
            ('codex', 'pro100', 'usage', r'Pro \$100: about \$(\d+)–(\d+) a week', midpoint,
             'midpoint of the measured weekly range at API prices'),
        ],
    },
    'community.eneasf.antigravity': {
        'file': 'community-eneasf-antigravity.md',
        'date': (r'On [A-Z][a-z]+ (\d{1,2} [A-Z][a-z]+ \d{4}), our test harness drove the first recorded empirical weekly', '%d %B %Y'),
        'readings': [
            ('antigravity', 'pro', 'gemini-5h', r'yields a \\?\$([\d.]+) USD 5-hour burst allowance', weekly,
             'Google AI Pro Gemini 5-hour allowance at Google API rates, calibrated against the empirical Connect-RPC '
             'limits of one telemetry harness (about 93% cache reads)'),
            ('antigravity', 'pro', 'gemini-weekly', r'USD 5-hour burst allowance and a \\?\$([\d.]+) USD weekly allowance', weekly,
             'Google AI Pro Gemini weekly allowance at Google API rates, calibrated against empirical limits '
             '(weekly exhaustion recorded by the harness)'),
            ('antigravity', 'pro', '3p-weekly', DIAGRAM, both_tracks(0),
             'Google AI Pro Claude/GPT track weekly capacity: the dual-track diagram states the same figure for both tracks'),
            ('antigravity', 'pro', '3p-5h', DIAGRAM, both_tracks(2),
             'Google AI Pro Claude/GPT track 5-hour capacity: the dual-track diagram states the same figure for both tracks'),
        ],
    },
}
NOT_FOUND = {
    'claude/max5': 'wokey is still collecting Max 5x accounts (no figure); finops gives only a bound (a Max 20x week is at '
                   'most about 1.5x a Max 5x week)',
    'antigravity/ultra5, ultra20': 'only Google AI Pro is measured (eneasf telemetry, September 2026); the same '
                                   'project sets Ultra 5x to 5x Pro by assumption (its ADR-041), which is not a measurement; '
                                   'forum reports give times to exhaustion only (discuss.ai.google.dev, March 2026)',
    'pi/*': 'Pi spends a Codex account allowance; the plan behind it is not known',
}


def page_date(text, spec):
    pattern, fmt = spec
    m = re.search(pattern, text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), fmt).date().isoformat()
    except ValueError:
        return None


def token_mix(text, spec, sid, row):
    """{cache_read, input, output} shares of all tokens, or None, from the fixed-split sentence and the Max 20x row."""
    split, mrow, share = (re.search(spec[k], text) for k in ('split', 'row', 'row_share'))
    if not (split and mrow and share):
        return None
    cache_read = int(split.group(2)) / 100
    non_cache, output = float(mrow.group(1)), float(mrow.group(2))
    rest = 1 - cache_read
    return {'cache_read': round(cache_read, 4), 'input': round(rest * (1 - output / non_cache), 5),
            'output': round(rest * output / non_cache, 5), 'split_week_to': split.group(1),
            'source': sid, 'kind': 'community', 'url': row['url'], 'as_of': row['as_of'], 'fetched_at': row['fetched_at'],
            'quotes': [split.group(0), mrow.group(0)[:160], share.group(0)],
            'method': ('cache-read share as stated for the fixed split; the other tokens split into input (uncached input '
                       'and cache writes, priced at the input price) and output by the Max 20x row measured at that split '
                       f'({non_cache:g}M non-cache-read, {output:g}M output a week)')}


def extract(target, manifest):
    """(sources {id: row}, observations, rules, errors, mix). An error has `level` red (no value from it) or yellow."""
    rows, obs, rules, errors, mix = {}, [], [], [], None
    for sid, spec in SOURCES.items():
        meta = manifest.get(spec['file']) or {}
        path = target / spec['file']
        url = meta.get('url') or PAGES[spec['file']]
        if meta.get('status') != 'ok' or not path.exists():
            errors.append({'source': sid, 'url': url, 'level': 'red', 'reason': 'source_missing',
                           'detail': meta.get('error') or 'not downloaded',
                           'effect': 'no value from this source; windows it fed fall back to their other sources or unknown'})
            continue
        text = sources._text(path.read_text(encoding='utf-8', errors='ignore'))
        as_of = page_date(text, spec['date'])
        rows[sid] = {'file': spec['file'], 'url': url, 'fetched_at': meta.get('fetched_at'), 'sha256': meta.get('sha256'),
                     'as_of': as_of or (meta.get('fetched_at') or '')[:10],
                     'as_of_kind': 'data date' if as_of else 'fetch date (no data date found on the page)',
                     'kind': 'community', 'models_listed': None,
                     'method': 'community real-usage measurement of subscription window capacity (calibration/community.py)'}
        if not as_of:
            errors.append({'source': sid, 'url': url, 'level': 'yellow', 'reason': 'no_data_date',
                           'effect': 'its age is counted from the fetch date'})
        for family, plan, window, pattern, value, method in spec['readings']:
            m = re.search(pattern, text)
            try:
                figure = value(m.groups()) if m else None
                detail = None if m else 'the page no longer carries the sentence'
            except ValueError as exc:
                figure, detail = None, str(exc)
            if figure is None:
                errors.append({'source': sid, 'url': url, 'level': 'red', 'reason': 'reading_not_found',
                               'family': family, 'plan': plan, 'window': window, 'detail': detail,
                               'effect': 'no value from it'})
                continue
            obs.append({'family': family, 'plan': plan, 'window': window, 'value': round(figure, 2),
                        'source': sid, 'kind': 'community', 'url': url, 'as_of': rows[sid]['as_of'],
                        'fetched_at': rows[sid]['fetched_at'], 'method': method, 'quote': m.group(0)[:200]})
        if spec.get('mix'):
            mix = token_mix(text, spec['mix'], sid, rows[sid])
            if mix is None:
                errors.append({'source': sid, 'url': url, 'level': 'red', 'reason': 'mix_not_found',
                               'effect': 'no community token mix: usd per hour on the community_mix basis is missing'})
        for rule in spec.get('rules', ()):
            m = re.search(rule['pattern'], text)
            if not m:
                errors.append({'source': sid, 'url': url, 'level': 'red', 'reason': 'statement_not_found',
                               'family': rule['family'], 'window': rule['window'],
                               'effect': 'the window is not derived from this statement'})
                continue
            rules.append({k: v for k, v in rule.items() if k != 'pattern'} |
                         {'source': sid, 'kind': 'community', 'url': url, 'as_of': rows[sid]['as_of'],
                          'fetched_at': rows[sid]['fetched_at']})
    return rows, obs, rules, errors, mix
