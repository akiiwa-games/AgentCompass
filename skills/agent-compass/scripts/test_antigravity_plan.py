import base64
from datetime import datetime, timedelta, timezone
import json
import unittest
import urllib.error

import antigravity_plan as plan
from router_test_support import temporary_root


# Token expiries must be in the future by the wall clock (read_token checks them against it).
TOKEN_BASE = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=2)
LIFETIME = 3600


def signed_in(email, generation=0, lifetime=LIFETIME, iat_exp=True):
    """agy's keyring blob for `email`'s token number `generation`: every renewal moves `expiry` on by one lifetime, and
    the id_token carries `iat`/`exp` one lifetime apart unless `iat_exp` is False."""
    expiry = TOKEN_BASE + timedelta(seconds=generation * lifetime)
    claims = {'email': email, **({'iat': int(expiry.timestamp()) - lifetime, 'exp': int(expiry.timestamp())} if iat_exp else {})}
    return lambda: blob(expiry, claims)


def blob(expiry, claims=None):
    token = {'access_token': 'SECRET', 'expiry': expiry.isoformat()}
    if claims:
        encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        token['id_token'] = f'e30.{encoded}.sig'
    return json.dumps({'token': token, 'auth_method': 'consumer'})


FREE = {'currentTier': {'id': 'free-tier', 'name': 'Antigravity'}}


ULTRA = {'currentTier': {'id': 'free-tier', 'name': 'Antigravity'},
         'paidTier': {'id': 'g1-ultra-tier', 'name': 'Google AI Ultra'}}


class AntigravityPlanTests(unittest.TestCase):
    def setUp(self):
        self.state = temporary_root(self.addCleanup)
        self.fresh = lambda: blob(datetime.now(timezone.utc) + timedelta(minutes=40))

    def test_paid_tier_is_read_with_agy_token_and_cached(self):
        seen = []
        document = plan.refresh(self.state, read_blob=self.fresh, fetch=lambda token, timeout: seen.append(token) or ULTRA)
        self.assertEqual(seen, ['SECRET'])
        self.assertEqual((document['plan'], document['tier_id'], document['source']), ('Google AI Ultra', 'g1-ultra-tier', 'paidTier'))
        self.assertEqual(plan.cached_plan(self.state), 'Google AI Ultra')
        self.assertNotIn('SECRET', (self.state / plan.CACHE_FILE).read_text(encoding='utf-8'))

    def test_current_tier_is_used_without_a_paid_tier(self):
        response = {'currentTier': {'id': 'standard-tier', 'name': 'Antigravity'}}
        self.assertEqual(plan.tier_from(response)['tier_id'], 'standard-tier')
        self.assertIsNone(plan.tier_from({}))

    def test_failures_keep_the_last_good_plan_and_retry_sooner(self):
        plan.refresh(self.state, read_blob=self.fresh, fetch=lambda *_: ULTRA)
        expired = lambda: blob(datetime.now(timezone.utc) - timedelta(minutes=1))
        document = plan.refresh(self.state, read_blob=expired, fetch=lambda *_: self.fail('no call with an expired token'))
        self.assertEqual((document['plan'], document['error']), ('Google AI Ultra', 'agy_token_expired'))

        def rejected(*_):
            raise urllib.error.HTTPError(plan.ENDPOINT, 401, 'unauthorized', {}, None)
        document = plan.refresh(self.state, read_blob=self.fresh, fetch=rejected)
        self.assertEqual((document['plan'], document['error']), ('Google AI Ultra', 'http_401'))
        self.assertEqual(plan.cached_plan(self.state), 'Google AI Ultra')

        checked = datetime.now(timezone.utc)
        failed = {'plan': 'Google AI Ultra', 'error': 'http_401', 'checked_at': plan._stamp(checked)}
        self.assertFalse(plan.due(failed, checked + timedelta(minutes=10)))
        self.assertTrue(plan.due(failed, checked + timedelta(minutes=plan.RETRY_MINUTES)))
        good = {'plan': 'Google AI Ultra', 'checked_at': plan._stamp(checked)}
        self.assertFalse(plan.due(good, checked + timedelta(hours=plan.INTERVAL_HOURS - 1)))
        self.assertTrue(plan.due(good, checked + timedelta(hours=plan.INTERVAL_HOURS)))
        self.assertTrue(plan.due({}))

    def test_a_lost_sign_in_drops_the_old_tier_at_once(self):
        plan.refresh(self.state, read_blob=self.fresh, fetch=lambda *_: ULTRA)
        self.assertEqual(plan.refresh(self.state, read_blob=lambda: None)['error'], 'agy_not_signed_in')
        self.assertIsNone(plan.cached_plan(self.state))

    def test_a_tier_expires_when_it_cannot_be_read_again(self):
        now = datetime(2026, 11, 7, tzinfo=timezone.utc)
        plan.refresh(self.state, read_blob=self.fresh, fetch=lambda *_: ULTRA, now=now)
        inside, outside = timedelta(hours=plan.PLAN_MAX_AGE_HOURS - 1), timedelta(hours=plan.PLAN_MAX_AGE_HOURS)
        self.assertEqual(plan.cached_plan(self.state, now + inside), 'Google AI Ultra')
        self.assertIsNone(plan.cached_plan(self.state, now + outside))

    def test_a_tier_with_no_paid_plan_reads_as_free(self):
        now = plan._now()
        (self.state / plan.CACHE_FILE).write_text(json.dumps({
            'plan': 'Antigravity', 'tier_id': 'free-tier', 'source': 'currentTier', 'read_at': plan._stamp(now),
            'free_confirmed': True}), encoding='utf-8')
        self.assertEqual(plan.cached_plan(self.state), 'Free')

    def test_a_free_tier_reported_as_the_paid_tier_still_reads_as_free(self):
        # 2026-11-10: loadCodeAssist returned `paidTier: {id: free-tier, name: Antigravity Starter Quota}`.
        now = datetime(2026, 11, 10, tzinfo=timezone.utc)
        response = {'paidTier': {'id': 'free-tier', 'name': 'Antigravity Starter Quota'}}
        token, renewed = signed_in('only@example.com'), signed_in('only@example.com', generation=1)
        plan.refresh(self.state, read_blob=token, fetch=lambda *_: response, now=now)
        self.assertEqual(plan.read_cache(self.state)['source'], 'paidTier')
        self.assertIsNone(plan.cached_plan(self.state, now))      # one free answer is not yet a lapse
        later = now + timedelta(minutes=5)
        plan.refresh(self.state, read_blob=token, fetch=lambda *_: response, now=later)
        self.assertIsNone(plan.cached_plan(self.state, later))    # the same token again confirms nothing
        self.assertFalse(plan.due(plan.read_cache(self.state), later, reading=plan.reading_of(token())))
        self.assertTrue(plan.due(plan.read_cache(self.state), later, reading=plan.reading_of(renewed())))
        plan.refresh(self.state, read_blob=renewed, fetch=lambda *_: response, now=later)
        self.assertEqual(plan.cached_plan(self.state, later), 'Free')
        # standard-tier can be a paid Code Assist plan: it keeps the name Google gives it.
        response = {'paidTier': {'id': 'standard-tier', 'name': 'Gemini Code Assist Standard'}}
        plan.refresh(self.state, read_blob=renewed, fetch=lambda *_: response, now=later)
        self.assertEqual(plan.cached_plan(self.state, later), 'Gemini Code Assist Standard')

    def test_the_cache_records_a_digest_of_the_keyring_account_never_the_address(self):
        now = datetime(2026, 11, 11, tzinfo=timezone.utc)
        plan.refresh(self.state, read_blob=signed_in('Paid@Example.com'), fetch=lambda *_: ULTRA, now=now)
        text = (self.state / plan.CACHE_FILE).read_text(encoding='utf-8')
        document = json.loads(text)
        self.assertEqual(document['identity'], plan.identity_of(signed_in('paid@example.com')()))
        self.assertTrue(document['identity'].startswith('sha256:'))
        for secret in ('SECRET', 'example.com', 'Paid'):
            self.assertNotIn(secret, text)

    def test_another_account_in_the_slot_is_re_read_at_once_and_drops_the_old_tier(self):
        now = datetime(2026, 11, 11, tzinfo=timezone.utc)
        paid = signed_in('paid@example.com')
        plan.refresh(self.state, read_blob=paid, fetch=lambda *_: ULTRA, now=now)
        document = plan.read_cache(self.state)
        later = now + timedelta(minutes=1)
        self.assertFalse(plan.due(document, later, reading=plan.reading_of(paid())))
        self.assertTrue(plan.due(document, later, reading=plan.reading_of(signed_in('free@example.com')())))
        # One token lifetime later the new account's own tier replaces the old one; the old account's plan never carries over.
        after = now + timedelta(seconds=LIFETIME + 60)
        plan.refresh(self.state, read_blob=signed_in('free@example.com'), fetch=lambda *_: FREE, now=after)
        document = plan.read_cache(self.state)
        self.assertEqual((document['tier_id'], document['identity']),
                         ('free-tier', plan.identity_of(signed_in('free@example.com')())))
        self.assertIsNone(plan.flapping(document, after))
        self.assertIsNone(plan.cached_plan(self.state, after))     # unconfirmed free answer

    def test_two_accounts_within_the_window_are_flapping_and_no_tier_counts(self):
        # Round 059: the free account's processes refresh at :38, the paid account's at :50.
        start = datetime(2026, 11, 11, 3, 38, tzinfo=timezone.utc)
        free, paid = signed_in('free@example.com'), signed_in('paid@example.com')
        plan.refresh(self.state, read_blob=free, fetch=lambda *_: FREE, now=start)
        self.assertIsNone(plan.cached_plan(self.state, start))
        flip = start + timedelta(minutes=12)
        plan.refresh(self.state, read_blob=paid, fetch=lambda *_: ULTRA, now=flip)
        state = plan.cached_state(self.state, flip)
        self.assertEqual(state['plan'], None)
        # The window is the token's own lifetime (id_token exp - iat), not a set number of minutes.
        self.assertEqual((state['flapping']['accounts'], state['flapping']['window_minutes']), (2, LIFETIME / 60))
        # A free answer at the next flip back, on a renewed token, is still not a lapse: the account is flapping.
        back = start + timedelta(minutes=60)
        free_renewed = signed_in('free@example.com', generation=1)
        plan.refresh(self.state, read_blob=free_renewed, fetch=lambda *_: FREE, now=back)
        self.assertIsNone(plan.cached_plan(self.state, back))
        self.assertTrue(plan.flapping(plan.read_cache(self.state), back))
        self.assertTrue(plan.due(plan.read_cache(self.state), back, reading=plan.reading_of(signed_in('free@example.com', 2)())))
        # Once one account has held the slot for a whole lifetime, its tier counts again.
        settled = back + timedelta(seconds=LIFETIME + 60)
        plan.refresh(self.state, read_blob=signed_in('paid@example.com', generation=2), fetch=lambda *_: ULTRA, now=settled)
        self.assertIsNone(plan.flapping(plan.read_cache(self.state), settled))
        self.assertEqual(plan.cached_plan(self.state, settled), 'Google AI Ultra')

    def test_without_iat_exp_the_lifetime_is_the_step_between_two_tokens_and_until_then_a_data_error(self):
        now = datetime(2026, 11, 11, tzinfo=timezone.utc)
        first = signed_in('only@example.com', lifetime=1800, iat_exp=False)
        document = plan.refresh(self.state, read_blob=first, fetch=lambda *_: ULTRA, now=now)
        self.assertNotIn('lifetime_seconds', document)
        self.assertIn(plan.LIFETIME_UNREADABLE, plan.cached_state(self.state, now)['data_error'])
        renewed = signed_in('only@example.com', generation=1, lifetime=1800, iat_exp=False)
        document = plan.refresh(self.state, read_blob=renewed, fetch=lambda *_: ULTRA, now=now + timedelta(minutes=30))
        self.assertEqual(document['lifetime_seconds'], 1800)
        self.assertIsNone(plan.cached_state(self.state, now + timedelta(minutes=30))['data_error'])

    def test_background_refresh_needs_agy_and_honours_the_switch(self):
        self.assertIsNone(plan.refresh_in_background(self.state, {'PATH': ''}))
        self.assertIsNone(plan.refresh_in_background(self.state, {'AGENTCOMPASS_ANTIGRAVITY_PLAN': 'off'}))


if __name__ == '__main__':
    unittest.main()
