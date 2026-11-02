"""Grok usage comes from the CLI's billing endpoint read-only; ~/.grok/auth.json is never written."""
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import usage_core as usage

PAYLOAD = {"config": {"currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "start": "2026-10-27T08:26:58.149+00:00",
                                        "end": "2026-11-03T08:26:58.149+00:00"},
                      "creditUsagePercent": 75.0, "productUsage": [{"product": "GrokBuild", "usagePercent": 71.0}],
                      "billingPeriodEnd": "2026-11-03T08:26:58.149+00:00"}}


class Response(io.BytesIO):
    status = 200
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def login(expires: datetime) -> dict:
    return {"https://auth.x.ai::client": {"key": "access", "refresh_token": "single-use", "user_id": "u1",
                                          "expires_at": expires.isoformat().replace("+00:00", "Z")}}


class GrokCliBillingTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        patcher = patch.object(usage, "GROK_HOME", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_login(self, expires):
        (self.home / "auth.json").write_text(json.dumps(login(expires)), encoding="utf-8")

    def test_the_overall_credit_percent_wins_over_product_shares(self):
        self.assertEqual(usage.parse_grok_cli_billing(PAYLOAD), (75.0, "2026-11-03T08:26:58.149000+00:00"))

    def test_an_omitted_percent_is_zero_only_on_the_current_period_shape(self):
        self.assertEqual(usage.parse_grok_cli_billing({"config": {"currentPeriod": {"end": "2026-11-03T00:00:00Z"}}})[0], 0.0)
        self.assertEqual(usage.parse_grok_cli_billing({"config": {"monthlyLimit": {"val": 2000}, "used": {"val": 500}}})[0], 25.0)
        for bad in ({}, {"config": {}}, {"config": {"creditUsagePercent": -1}}, {"config": {"creditUsagePercent": True}}):
            with self.assertRaises(ValueError):
                usage.parse_grok_cli_billing(bad)

    def test_an_unexpired_login_reads_the_cli_endpoint(self):
        self.write_login(datetime.now(timezone.utc) + timedelta(hours=1))
        with patch.object(usage.urllib.request, "urlopen", return_value=Response(json.dumps(PAYLOAD).encode())) as call,                 patch.object(usage, "cache_put_provider"):
            result = usage.fetch_grok_usage()
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, usage.GROK_CLI_BILLING_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer access")
        self.assertTrue(result.available)
        self.assertEqual([(w.id, w.used_pct) for w in result.windows], [("credits", 75.0)])

    def test_an_expired_login_is_neither_refreshed_nor_rewritten(self):
        self.write_login(datetime.now(timezone.utc) - timedelta(minutes=1))
        before = (self.home / "auth.json").read_bytes()
        with patch.object(usage.urllib.request, "urlopen") as call:
            self.assertIsNone(usage.fetch_grok_cli_billing())
        call.assert_not_called()
        self.assertEqual((self.home / "auth.json").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["auth.json"])

    def test_no_login_file_keeps_the_environment_token_fallback(self):
        with patch.dict(usage.os.environ, {"GROK_TOKEN": "env-token"}),                 patch.object(usage.urllib.request, "urlopen", side_effect=OSError("offline")) as call:
            result = usage.fetch_grok_usage()
        self.assertEqual([c.args[0].full_url for c in call.call_args_list], [usage.GROK_BILLING_URL])
        self.assertEqual(call.call_args.args[0].get_header("Authorization"), "Bearer env-token")
        self.assertFalse(result.available)

    def test_a_cli_failure_falls_back_to_the_grok_com_endpoint(self):
        self.write_login(datetime.now(timezone.utc) + timedelta(hours=1))
        with patch.object(usage.urllib.request, "urlopen", side_effect=OSError("offline")) as call:
            usage.fetch_grok_usage()
        self.assertEqual([c.args[0].full_url for c in call.call_args_list], [usage.GROK_CLI_BILLING_URL, usage.GROK_BILLING_URL])


if __name__ == "__main__":
    unittest.main()
