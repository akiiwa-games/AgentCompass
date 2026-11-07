from datetime import datetime, timedelta, timezone
import json
import unittest
import urllib.error

import antigravity_plan as plan
from router_test_support import temporary_root


def blob(expiry):
    return json.dumps({'token': {'access_token': 'SECRET', 'expiry': expiry.isoformat()}, 'auth_method': 'consumer'})


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
        (self.state / plan.CACHE_FILE).write_text(json.dumps({
            'plan': 'Antigravity', 'tier_id': 'free-tier', 'source': 'currentTier', 'read_at': plan._stamp(plan._now())}),
            encoding='utf-8')
        self.assertEqual(plan.cached_plan(self.state), 'Free')

    def test_background_refresh_needs_agy_and_honours_the_switch(self):
        self.assertIsNone(plan.refresh_in_background(self.state, {'PATH': ''}))
        self.assertIsNone(plan.refresh_in_background(self.state, {'AGENTCOMPASS_ANTIGRAVITY_PLAN': 'off'}))


if __name__ == '__main__':
    unittest.main()
