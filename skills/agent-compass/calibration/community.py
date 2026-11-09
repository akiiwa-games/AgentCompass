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

Not found (no community measurement published as of 2026-11-09; listed, not invented): Claude Max 5x (wokey is still
collecting), every Antigravity window (Google AI Pro / Ultra), Pi (spends a Codex allowance of an unknown plan).
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
}
NOT_FOUND = {
    'claude/max5': 'wokey is still collecting Max 5x accounts (no figure); finops gives only a bound (a Max 20x week is at '
                   'most about 1.5x a Max 5x week)',
    'antigravity/*': 'no community API-dollar measurement of Google AI Pro / Ultra Antigravity quota found',
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


def extract(target, manifest):
    """(sources {id: row}, observations, rules, errors). An error has `level` red (no value from it) or yellow."""
    rows, obs, rules, errors = {}, [], [], []
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
            if not m:
                errors.append({'source': sid, 'url': url, 'level': 'red', 'reason': 'reading_not_found',
                               'family': family, 'plan': plan, 'window': window,
                               'effect': 'the page no longer carries the sentence; no value from it'})
                continue
            obs.append({'family': family, 'plan': plan, 'window': window, 'value': round(value(m.groups()), 2),
                        'source': sid, 'kind': 'community', 'url': url, 'as_of': rows[sid]['as_of'],
                        'fetched_at': rows[sid]['fetched_at'], 'method': method, 'quote': m.group(0)[:200]})
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
    return rows, obs, rules, errors
