"""One state per account, read by dispatch and by every account list.

`ready`          the usage reading is good.
`plan_inactive`  the plan is free, expired, cancelled or unpaid, or a launch reported that: excluded at once.
`usage_failing`  the reading failed (any error, no reading): listed at once, dispatch carries a warning, and the
                 account is excluded once its last good reading is older than `exclude_after_hours` (default 6).
`auth_failed`    the provider refused the credentials (401/403): excluded at once.

A good reading returns the account to `ready`; nobody has to clear anything. Only provider and collector facts decide
a state, never task text. A launch's subscription error is latched in <state dir>/plan-inactive.json until a good
reading newer than the latch; no remote error text is stored.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

READY, PLAN_INACTIVE, USAGE_FAILING, AUTH_FAILED = 'ready', 'plan_inactive', 'usage_failing', 'auth_failed'
DEFAULT_EXCLUDE_AFTER_HOURS = 6.0
EXCLUDE_AFTER_ENV = 'AGENTCOMPASS_USAGE_FAILING_EXCLUDE_HOURS'
# Twice the longest polling cadence the router accepts (route_judge.POLL_MAX_SECONDS): a reading this old without an
# error means nothing is reading the account any more.
READING_TOO_OLD_SECONDS = 2 * 3600
# States an account list must keep showing even when the account cannot take work.
LISTED_STATES = (READY, PLAN_INACTIVE, USAGE_FAILING, AUTH_FAILED)
# Plan names that mean the subscription gives no quota, whichever source reported them.
INACTIVE_PLANS = frozenset(('free', 'free tier', 'expired', 'cancelled', 'canceled', 'inactive', 'lapsed', 'none',
                            'past due', 'payment failed', 'unpaid'))
# Words that name the vendor or product in a plan label, not the plan: "Antigravity Free Tier" is `free tier`.
PLAN_VENDOR_WORDS = frozenset(('antigravity', 'chatgpt', 'claude', 'google', 'ai', 'openai', 'plan'))
# Families whose account is a paid subscription: a free plan there is a lapse. OpenCode's free models are its product.
SUBSCRIPTION_FAMILIES = ('claude', 'codex', 'antigravity', 'pi', 'omp')
LATCH_FILE = 'plan-inactive.json'
# What the Paseo bridge reports (lastErrorClass, failure_class) or a provider's own error code. Structured fields decide
# when present; the text pattern is only for a report with none. A model-level refusal ("your plan does not include this
# model") never matches: the account still runs its other models.
INACTIVE_ERROR_CODES = frozenset(('subscription_inactive', 'subscription_expired', 'subscription_canceled',
                                  'subscription_cancelled', 'plan_expired', 'plan_inactive', 'payment_required',
                                  'payment_failed'))
_LAUNCH_ERROR = re.compile(
    r'subscription(?:_| (?:has |is |was )?)(?:expired|cancel+ed|inactive|lapsed)|'
    r'plan (?:has |is )?(?:expired|cancel+ed|inactive)|'
    r'payment[_ ](?:required|failed)|past[_ ]due|'
    r'订阅(?:已)?(?:过期|到期|失效|取消)', re.I)


def _time(value):
    try:
        moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def plan_words(label):
    return ' '.join(re.findall(r'[a-z0-9]+', str(label or '').lower()))


def plan_without_vendor(label):
    return ' '.join(word for word in plan_words(label).split() if word not in PLAN_VENDOR_WORDS)


def plan_is_inactive(family, plan):
    return family in SUBSCRIPTION_FAMILIES and plan_without_vendor(plan) in INACTIVE_PLANS


def reading(row, now):
    """(problem, last good reading's time) of a usage-cache row; problem None means the reading is good."""
    row = row if isinstance(row, dict) else {}
    windows = row.get('windows') if isinstance(row.get('windows'), list) else []
    numeric = any(isinstance(w, dict) and isinstance(w.get('used_pct'), (int, float)) for w in windows)
    fetched = _time(row.get('fetched_at'))
    last = fetched.isoformat() if fetched and numeric else None
    if not row:
        return 'no_reading', None
    if row.get('error'):
        return f"read_failed: {row['error']}", last
    if row.get('available') is False:
        return 'read_failed: provider unavailable', last
    if not numeric:
        return 'no_reading: usage came back empty', None
    if fetched is None:
        return 'no_reading: reading has no time', None
    if (now - fetched).total_seconds() > READING_TOO_OLD_SECONDS:
        return 'reading_too_old', last
    return None, last


def exclude_after_hours(environ=None, override_doc=None):
    """Hours a failing reading may stay dispatchable: the environment, then accounts-override `scoring`, then 6."""
    candidates = ((os.environ if environ is None else environ).get(EXCLUDE_AFTER_ENV),
                  ((override_doc or {}).get('scoring') or {}).get('usage_failing_exclude_hours'))
    for value in candidates:
        try:
            hours = float(value)
        except (TypeError, ValueError):
            continue
        if hours >= 0:
            return hours
    return DEFAULT_EXCLUDE_AFTER_HOURS


def failing(row, problem, last_success, previous, now, hours):
    """Set `usage_failing` on an inventory row. With no good reading on file the clock starts at the first failure seen."""
    since = previous.get('failing_since') if isinstance(previous, dict) and previous.get('status') == USAGE_FAILING else None
    since = since if _time(since) else now.isoformat()
    anchor = _time(last_success) or _time(since)
    row.update(status=USAGE_FAILING, reason=problem, failing_since=since, exclude_after_hours=hours,
               usage_excluded=(now - anchor).total_seconds() >= hours * 3600)


def dispatchable(row):
    """True while the account may take work: `ready`, or `usage_failing` still inside its grace period."""
    if not isinstance(row, dict):
        return False
    return row.get('status') == READY or (row.get('status') == USAGE_FAILING and not row.get('usage_excluded'))


def unavailable_text(row):
    """The exclusion reason of an account this module excluded; None for every other state."""
    status = row.get('status')
    if status == PLAN_INACTIVE:
        return f"plan inactive ({row.get('reason')}); renew the subscription or sign in to a paid account"
    if status == USAGE_FAILING and row.get('usage_excluded'):
        return (f"usage reading failing ({row.get('reason')}), last good reading "
                f"{row.get('last_success_at') or 'never'}, over the {row.get('exclude_after_hours'):g}h limit; "
                "run `agentcompass usage` or fix the account")
    if status == AUTH_FAILED:
        return f"credentials rejected ({row.get('reason')}); sign in again in the account's own CLI"
    return None


def warning(name, row):
    """The lines `plan`/`roll` show for one account; empty when nothing needs saying."""
    status = row.get('status')
    lines = []
    if status == USAGE_FAILING:
        state = 'excluded from dispatch' if row.get('usage_excluded') else (
            f"still dispatchable, excluded once the last good reading is {row.get('exclude_after_hours'):g}h old")
        lines.append(f"{name}: usage_failing ({row.get('reason')}), last good reading "
                     f"{row.get('last_success_at') or 'never'}; {state}")
    elif status == PLAN_INACTIVE:
        lines.append(f"{name}: plan_inactive ({row.get('reason')}); excluded from dispatch")
    elif status == AUTH_FAILED:
        lines.append(f"{name}: auth_failed ({row.get('reason')}); excluded from dispatch")
    conflict = row.get('plan_conflict')
    if isinstance(conflict, dict):
        lines.append(f"{name}: usage API reports plan {conflict.get('online')} but the credential says "
                     f"{conflict.get('credential')}; using {conflict.get('online')}")
    return lines


def warnings(inventory, specs=None, allowed=None):
    """Every account's state and plan warning, for `plan`/`roll`. `allowed(name)` skips accounts this host does not use."""
    found = []
    rows = (inventory or {}).get('accounts') or {}
    for name, row in rows.items():
        if not isinstance(row, dict) or (allowed is not None and not allowed(name)):
            continue
        found.extend(warning(name, row))
        spec = (specs or {}).get(name) or {}
        if spec.get('plan_unknown'):
            found.append(f"{name}: plan {spec['plan_unknown']!r} is not a known plan; priced at the conservative "
                         f"{spec.get('plan_priced_as')} capacity")
    return found


def latch_path(state_dir=None):
    root = state_dir or os.environ.get('ROUTER_STATE_DIR') or Path.home() / '.agent-compass'
    return Path(root) / LATCH_FILE


def _read_latches(path):
    try:
        document = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'accounts': {}}
    return document if isinstance(document, dict) and isinstance(document.get('accounts'), dict) else {'accounts': {}}


def error_means_inactive(error=None, code=None, status=None):
    if code or status:
        return status == 402 or str(code or '').strip().lower() in INACTIVE_ERROR_CODES
    return isinstance(error, str) and bool(_LAUNCH_ERROR.search(error))


def usage_refused_plan(row):
    """The usage API's own status or error code says the subscription has lapsed. Its text is not consulted."""
    return error_means_inactive(None, row.get('error_code'), row.get('error_status'))


def usage_refused_credentials(row, collector_words=()):
    """A 401/403 on the usage API. `collector_words` read a cached row written before the status was recorded."""
    status = row.get('error_status')
    return status in (401, 403) if status else row.get('error') in collector_words


def record_launch_error(account, error=None, *, code=None, status=None, at=None, source=None, state_dir=None):
    """Latch `plan_inactive` when a launch reports the subscription has lapsed. Returns True when it did."""
    if not error_means_inactive(error, code, status):
        return False
    import override_store
    path = latch_path(state_dir)
    at = at or datetime.now(timezone.utc).isoformat()
    with override_store.locked(path):
        document = _read_latches(path)
        held = _time((document['accounts'].get(account) or {}).get('observed_at'))
        if held and _time(at) and held >= _time(at):
            return False
        document['accounts'][account] = {'observed_at': at, 'source': source}
        override_store.write(path, document)
    return True


def latched(state_dir=None):
    return _read_latches(latch_path(state_dir))['accounts']


def clear_latch(account, *, state_dir=None, expected_observed_at=None):
    import override_store
    path = latch_path(state_dir)
    with override_store.locked(path):
        document = _read_latches(path)
        row = document['accounts'].get(account)
        if row is None or (expected_observed_at is not None and row.get('observed_at') != expected_observed_at):
            return False  # A newer launch failure outranks this recovery.
        del document['accounts'][account]
        override_store.write(path, document)
    return True


def latch_outlived(latch, last_good):
    """A good reading newer than the launch failure ends the latch."""
    observed, fetched = _time((latch or {}).get('observed_at')), _time(last_good)
    return bool(observed and fetched and fetched > observed)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clear', metavar='ACCOUNT', help='Clear one latched launch failure by hand')
    args = parser.parse_args()
    if args.clear:
        clear_latch(args.clear)
    print(json.dumps(latched(), ensure_ascii=False, indent=2))
