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
        for attr, val in (("GROK_HOME", self.home), ("HOME", self.home), ("CACHE_PATH", self.home / "usage-cache.json")):
            patcher = patch.object(usage, attr, val)
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

    def test_cli_and_web_failures_preserve_http_status_and_prefer_refusal_over_fallback_error(self):
        self.write_login(datetime.now(timezone.utc) + timedelta(hours=1))
        http_401 = usage.urllib.error.HTTPError(
            usage.GROK_CLI_BILLING_URL, 401, "Unauthorized", {},
            io.BytesIO(json.dumps({"error": {"code": "invalid_api_key"}}).encode()),
        )
        # CLI 401 followed by fallback OSError or missing fallback token must keep the 401 and error_code.
        with patch.object(usage.urllib.request, "urlopen", side_effect=[http_401, OSError("fallback failed")]):
            result = usage.fetch_grok_usage()
        self.assertEqual(result.error_status, 401)
        self.assertEqual(result.error_code, "invalid_api_key")
        row = usage.load_usage_cache()["providers"]["grok"]
        self.assertTrue(row["stale"])
        self.assertEqual(row["error_status"], 401)
        self.assertEqual(row["error_code"], "invalid_api_key")
        self.assertTrue(row.get("error_at"))

        # CLI 401 when read_grok_token() has no fallback token still records 401, not "no token".
        http_401_no_fallback = usage.urllib.error.HTTPError(
            usage.GROK_CLI_BILLING_URL, 401, "Unauthorized", {},
            io.BytesIO(json.dumps({"code": "token_revoked"}).encode()),
        )
        with patch.object(usage, "read_grok_token", return_value=None), \
                patch.object(usage.urllib.request, "urlopen", side_effect=http_401_no_fallback):
            result = usage.fetch_grok_usage()
        self.assertEqual(result.error_status, 401)
        self.assertEqual(result.error_code, "token_revoked")
        row = usage.load_usage_cache()["providers"]["grok"]
        self.assertEqual(row["error_status"], 401)
        self.assertEqual(row["error_code"], "token_revoked")

        # 402 payment_required on web fallback when CLI login is expired.
        self.write_login(datetime.now(timezone.utc) - timedelta(minutes=5))
        http_402 = usage.urllib.error.HTTPError(
            usage.GROK_BILLING_URL, 402, "Payment Required", {},
            io.BytesIO(json.dumps({"error": "payment_required"}).encode()),
        )
        with patch.dict(usage.os.environ, {"GROK_API_KEY": "env-token"}), \
                patch.object(usage.urllib.request, "urlopen", side_effect=http_402):
            result = usage.fetch_grok_usage()
        self.assertEqual(result.error_status, 402)
        self.assertEqual(result.error_code, "payment_required")
        row = usage.load_usage_cache()["providers"]["grok"]
        self.assertEqual(row["error_status"], 402)
        self.assertEqual(row["error_code"], "payment_required")

    def test_failure_after_existing_windows_retains_windows_and_records_stale_error_in_cache(self):
        self.write_login(datetime.now(timezone.utc) + timedelta(hours=1))
        with patch.object(usage.urllib.request, "urlopen", return_value=Response(json.dumps(PAYLOAD).encode())):
            first = usage.collect_local_usage(["grok"], force=True)[0]
        self.assertTrue(first.available)
        self.assertFalse(first.stale)
        self.assertEqual([(w.id, w.used_pct) for w in first.windows], [("credits", 75.0)])

        # Same credential fails with 500: windows stay for display, but stale/error/error_status/error_at are written.
        http_500 = usage.urllib.error.HTTPError(
            usage.GROK_CLI_BILLING_URL, 500, "Internal Server Error", {}, io.BytesIO(b"{}")
        )
        with patch.object(usage.urllib.request, "urlopen", side_effect=[http_500, OSError("offline")]):
            second = usage.collect_local_usage(["grok"], force=True)[0]
        self.assertTrue(second.stale)
        self.assertEqual(second.error_status, 500)
        self.assertEqual([(w.id, w.used_pct) for w in second.windows], [("credits", 75.0)])
        row = usage.load_usage_cache()["providers"]["grok"]
        self.assertTrue(row["stale"])
        self.assertEqual(row["error_status"], 500)
        self.assertTrue(row.get("error_at"))
        self.assertEqual([(w["id"], w["used_pct"]) for w in row["windows"]], [("credits", 75.0)])

        # Rotating credential invalidates the cached reading even without force=True and drops old windows on 401.
        rotated = login(datetime.now(timezone.utc) + timedelta(hours=1))
        rotated["https://auth.x.ai::client"]["key"] = "rotated-bad-key"
        (self.home / "auth.json").write_text(json.dumps(rotated), encoding="utf-8")
        http_401 = usage.urllib.error.HTTPError(
            usage.GROK_CLI_BILLING_URL, 401, "Unauthorized", {}, io.BytesIO(b"{}")
        )
        with patch.object(usage.urllib.request, "urlopen", side_effect=[http_401, http_401]):
            third = usage.collect_local_usage(["grok"], force=False)[0]
        self.assertFalse(third.available)
        self.assertEqual(third.error_status, 401)
        row = usage.load_usage_cache()["providers"]["grok"]
        self.assertEqual(row["error_status"], 401)
        self.assertEqual(row["windows"], [])

        # Restoring the original valid credential immediately re-fetches even with force=False
        self.write_login(datetime.now(timezone.utc) + timedelta(hours=1))
        with patch.object(usage.urllib.request, "urlopen", return_value=Response(json.dumps(PAYLOAD).encode())):
            fourth = usage.collect_local_usage(["grok"], force=False)[0]
        self.assertTrue(fourth.available)
        self.assertFalse(fourth.stale)
        row = usage.load_usage_cache()["providers"]["grok"]
        self.assertTrue(row["available"])
        self.assertFalse(row["stale"])
        self.assertEqual(len(row["windows"]), 1)
        self.assertNotIn("error_status", row)

    def test_missing_grok_credentials_does_not_write_failure_row_to_cache(self):
        (self.home / "auth.json").write_text("{}", encoding="utf-8")
        with patch.dict(usage.os.environ, {"GROK_API_KEY": "", "GROK_TOKEN": ""}, clear=False), \
                patch.object(usage.urllib.request, "urlopen") as call:
            result = usage.collect_local_usage(["grok"], force=True)[0]
        call.assert_not_called()
        self.assertFalse(result.available)
        self.assertIsNone(result.error_status)
        self.assertNotIn("grok", usage.load_usage_cache().get("providers", {}))

    def test_auth_json_takes_precedence_over_env_in_token_and_credential_key(self):
        self.write_login(datetime.now(timezone.utc) + timedelta(hours=1))
        with patch.dict(usage.os.environ, {"GROK_API_KEY": "", "GROK_TOKEN": ""}, clear=False):
            key_file_only = usage.provider_credential_key("grok")
            tok_file_only = usage.read_grok_token()
        self.assertEqual(tok_file_only, "access")
        with patch.dict(usage.os.environ, {"GROK_API_KEY": "env-override-key", "GROK_TOKEN": "env-token"}, clear=False):
            self.assertEqual(usage.read_grok_token(), "access")
            self.assertEqual(usage.provider_credential_key("grok"), key_file_only)
        (self.home / "auth.json").write_text("{}", encoding="utf-8")
        with patch.dict(usage.os.environ, {"GROK_API_KEY": "env-override-key", "GROK_TOKEN": ""}, clear=False):
            self.assertEqual(usage.read_grok_token(), "env-override-key")
            self.assertIsNotNone(usage.provider_credential_key("grok"))
            self.assertNotEqual(usage.provider_credential_key("grok"), key_file_only)


if __name__ == "__main__":
    unittest.main()
