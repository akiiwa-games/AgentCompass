"""Ability floors per difficulty from public success rates on the tier scale, plus the evidence checks around them.

`python -X utf8 floors.py [--data DIR]`

- Floor estimate: the tier at which a model's expected success reaches 90% of the best measured model's (at most a
  tenth more failures). Binomial logistic fit of success on tier per class, read at that crossing, with model-resampling
  bootstrap 10/50/90 percentiles. A shipped floor moves only when the band excludes its current value.
- Classes: SWE-bench Verified (vals.ai) fix-time buckets: routine = <15 min, hard = 15 min-1 h, coupled = 1-4 h;
  Terminal-Bench 2.0 for hard tool work; LMArena Agent boards (net improvement in confirmed success) as a cross-check.
- Frontier: SWE-bench's >4 h bucket and the models statistically tied with the best on LMArena WebDev/Agent.
- Anchor contradictions: where public boards order models against the user's anchor tiers (reported, never applied).
"""
import argparse
import sys

import numpy as np

import sources
import tiers

X = 0.10
SWE_CLASSES = (('routine', '<15min'), ('hard', '15m–1h'), ('coupled', '1–4hr'))


def logistic(t, k, n, iters=200):
    """Binomial logistic regression success ~ a + b*tier by Newton-IRLS."""
    a = np.c_[np.ones_like(t), t]
    w = np.zeros(2)
    for _ in range(iters):
        p = 1 / (1 + np.exp(-a @ w))
        g = a.T @ (k - n * p)
        h = a.T @ (a * (n * p * (1 - p))[:, None])
        w += np.linalg.solve(h + 1e-9 * np.eye(2), g)
    return w


def crossing(t, k, n, rng, boot=2000):
    target = (1 - X) * (k / n).max()
    logit = np.log(target / (1 - target))
    out = []
    for _ in range(boot):
        i = rng.integers(0, len(t), len(t))
        a, b = logistic(t[i], k[i], n[i])
        if b > 0:
            out.append((logit - a) / b)
    return np.percentile(out, [10, 50, 90])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data')
    args = ap.parse_args()
    target = sources.data_dir(args.data)
    acc = sources.accounts()
    fitter = tiers.Fitter(target, acc)
    prim = {''.join(ch for ch in n.lower() if ch.isalnum()): t for n, t in fitter.primary_tier_by_base().items()}
    key = lambda name: ''.join(ch for ch in sources.base_name(name).lower() if ch.isalnum())
    rng = np.random.default_rng(7)
    shipped = acc['scoring']['ability_floor']

    _, buckets = sources.vals(target, 'swebench.html')
    rows = [(prim[key(m)], b) for m, b in buckets.items() if key(m) in prim]
    t = np.array([r[0] for r in rows])
    print(f'SWE-bench Verified (vals.ai), {len(rows)} models with an LMArena tier')
    for difficulty, bucket in SWE_CLASSES:
        k = np.array([r[1][bucket][0] for r in rows], float)
        n = np.array([r[1][bucket][1] for r in rows], float)
        band = np.round(crossing(t, k, n, rng), 2)
        moves = not (band[0] <= shipped[difficulty] <= band[2])
        print(f'  {difficulty:8s} {bucket:7s} best {(k / n).max():.2f}  crossing 10/50/90 {band}  shipped {shipped[difficulty]}'
              f"{'  -> outside the band: move to the median' if moves else ''}")
    over4 = [(prim[key(m)], b['>4hr']) for m, b in buckets.items() if key(m) in prim and '>4hr' in b]
    print(f"  frontier >4hr: {over4[0][1][1] if over4 else 0} tasks per model; {sum(1 for _, (a, n) in over4 if a == n)}/{len(over4)} "
          'models solve all of them, so the bucket cannot separate tiers (no frontier floor from SWE-bench)')

    tb2, _ = sources.vals(target, 'terminal-bench-2.html')
    tb = [(prim[key(m)], s) for m, s in tb2.items() if key(m) in prim]
    tt = np.array([r[0] for r in tb])
    k = np.round(np.array([r[1] for r in tb]) / 100 * 89)
    n = np.full(len(tb), 89.0)
    route = (acc['capability_policy']['routes']['tool_execution'].get('ability_floor') or {}).get('hard')
    print(f'Terminal-Bench 2.0 (vals.ai, 89 tasks), {len(tb)} models: hard tool crossing {np.round(crossing(tt, k, n, rng), 2)}'
          f'  shipped tool_execution hard floor {route}')

    print('Frontier: models statistically tied with the best (CI upper >= best CI lower)')
    for board, category in (('webdev', 'overall'), ('agent', 'overall'), ('text_style_control', 'expert'),
                            ('text_style_control', 'hard_prompts')):
        b = sources.arena(target, board, category)
        best = max(b.values(), key=lambda r: r[0])
        tied = sorted((r for r in b.values() if r[2] >= best[1]), key=lambda r: -r[0])
        print(f'  {board}/{category}: ' + '; '.join(r[3] for r in tied))
    print(f"  shipped frontier floor {shipped['frontier']} (the lowest top anchor tier a vendor subscription ships)")

    print('Anchor contradictions (reported, not applied)')
    flash, sol = 'Gemini 3.8 Flash', 'GPT-6.1 Sol'
    for board, category in (('text_style_control', 'hard_prompts'), ('text_style_control', 'instruction_following'),
                            ('vision', 'overall')):
        b = sources.arena(target, board, category)
        if flash in b and sol in b:
            print(f'  {board}/{category}: {b[flash][3]} {b[flash][0]:.0f} [{b[flash][1]:.0f},{b[flash][2]:.0f}] vs '
                  f'{b[sol][3]} {b[sol][0]:.0f} [{b[sol][1]:.0f},{b[sol][2]:.0f}]  (anchors: Sol 3.6 > Flash 3.0)')
    swe, _ = sources.vals(target, 'swebench.html')
    pairs = [(prim[key(m)], s) for m, s in swe.items() if key(m) in prim]
    slope, icept = np.polyfit([p[0] for p in pairs], [p[1] for p in pairs], 1)
    for name in ('GPT-5.6 Terra', 'GPT-5.6 Luna', 'GPT-5.6 Sol'):
        if name in swe and key(name) in prim:
            expect = icept + slope * prim[key(name)]
            print(f'  SWE-bench {name}: {swe[name]:.1f}% vs {expect:.1f}% expected at its LMArena tier {prim[key(name)]:.2f}')


if __name__ == '__main__':
    sys.exit(main())
