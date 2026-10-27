import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import account_login
import gateways


class Recorder:
    """Stands in for the gateways' HTTP APIs: answers by (method, path) and records each request."""
    def __init__(self, answers):
        self.answers, self.requests = answers, []

    def __call__(self, request, timeout=0):
        path = request.full_url.split("://", 1)[1].split("/", 1)[1]
        self.requests.append((request.get_method(), "/" + path, request.data, dict(request.header_items())))
        return self.answers[(request.get_method(), "/" + path)]


class GrokImportTests(unittest.TestCase):
    def test_sse_events_split_named_events(self):
        text = ": connected\n\nevent: progress\ndata: {\"n\":1}\n\nevent: complete\ndata: {\"created\":1}\n\n"
        self.assertEqual(account_login.sse_events(text), [("progress", '{"n":1}'), ("complete", '{"created":1}')])

    def test_imports_the_sso_token_as_a_text_file_with_the_admin_bearer(self):
        login = json.dumps({"data": {"tokens": {"accessToken": "tok"}}})
        stream = 'event: complete\ndata: {"created":1,"updated":0,"skipped":0,"synced":1,"syncFailed":0}\n\n'
        api = Recorder({("POST", "/api/admin/v1/auth/login"): (200, login),
                        ("POST", "/api/admin/v1/accounts/web/import"): (200, stream)})
        with mock.patch.object(gateways, "grok_admin", return_value=("admin", "boot")), \
                mock.patch.object(account_login, "_call", api):
            message = account_login.grok_import_sso("SSO-VALUE")
        self.assertIn("1", message)
        _, _, body, _ = api.requests[0]
        self.assertEqual(json.loads(body), {"username": "admin", "password": "boot"})
        _, _, body, headers = api.requests[1]
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertIn(b'name="files"; filename="sso.txt"', body)
        self.assertIn(b"\r\n\r\nSSO-VALUE\n\r\n", body)

    def test_a_changed_password_asks_for_it_and_the_given_one_is_used(self):
        api = Recorder({("POST", "/api/admin/v1/auth/login"): (401, '{"error":{"code":"invalidCredentials"}}')})
        with mock.patch.object(gateways, "grok_admin", return_value=("admin", "boot")), \
                mock.patch.object(account_login, "_call", api):
            with self.assertRaises(account_login.PasswordNeeded):
                account_login.grok_import_sso("x")
            with self.assertRaises(account_login.PasswordNeeded):
                account_login.grok_import_sso("x", password="typed")
        self.assertEqual(json.loads(api.requests[1][2])["password"], "typed")

    def test_an_error_event_fails_the_import(self):
        api = Recorder({("POST", "/api/admin/v1/auth/login"): (200, json.dumps({"data": {"tokens": {"accessToken": "t"}}})),
                        ("POST", "/api/admin/v1/accounts/web/import"):
                            (200, 'event: error\ndata: {"error":{"message":"bad sso"}}\n\n')})
        with mock.patch.object(gateways, "grok_admin", return_value=("admin", "p")), \
                mock.patch.object(account_login, "_call", api):
            with self.assertRaisesRegex(RuntimeError, "bad sso"):
                account_login.grok_import_sso("x")


class MuseImportTests(unittest.TestCase):
    COOKIES = [{"name": "hatch_sess", "value": "abcdEFGHIJKLwxyz", "expires": 1900000000.5},
               {"name": "hatch_native_auth_device", "value": "dev", "expires": -1}]

    def test_a_new_session_is_added_with_its_expiries(self):
        api = Recorder({("GET", "/admin/accounts"): (200, json.dumps({"data": []})),
                        ("POST", "/admin/accounts"): (200, json.dumps({"account": {"id": "a1"}, "missing_cookies": []}))})
        with mock.patch.object(gateways, "muse_api_key", return_value="KEY"), \
                mock.patch.object(account_login, "_call", api):
            message = account_login.muse_import(self.COOKIES, label="me")
        self.assertIn("me", message)
        method, path, body, headers = api.requests[1]
        self.assertEqual((method, path, headers["Authorization"]), ("POST", "/admin/accounts", "Bearer KEY"))
        payload = json.loads(body)
        self.assertEqual(payload["cookies"]["hatch_sess"], "abcdEFGHIJKLwxyz")
        self.assertEqual(payload["cookie_expires"], {"hatch_sess": 1900000000})

    def test_the_same_session_refreshes_the_existing_account_instead_of_duplicating_it(self):
        listed = {"data": [{"id": "old", "label": "mine", "cookies": {"hatch_sess": "abcd...wxyz"}}]}
        api = Recorder({("GET", "/admin/accounts"): (200, json.dumps(listed)),
                        ("PATCH", "/admin/accounts/old"): (200, "{}")})
        with mock.patch.object(gateways, "muse_api_key", return_value="KEY"), \
                mock.patch.object(account_login, "_call", api):
            message = account_login.muse_import(self.COOKIES)
        self.assertIn("mine", message)
        self.assertEqual([r[0] for r in api.requests], ["GET", "PATCH"])


class SessionWaitTests(unittest.TestCase):
    def test_waits_until_every_required_cookie_exists_then_returns_the_site_cookies(self):
        readings = iter([[], [{"name": "sso", "value": "", "domain": ".grok.com"}],
                         [{"name": "sso", "value": "v", "domain": ".grok.com"}]])
        with mock.patch.object(account_login, "open_page"), \
                mock.patch.object(account_login, "site_cookies", side_effect=lambda d: next(readings)), \
                mock.patch.object(account_login.time, "sleep"):
            cookies = account_login.wait_for_session("grok2api")
        self.assertEqual(cookies[0]["value"], "v")

    def test_closing_the_window_stops_waiting(self):
        with mock.patch.object(account_login, "open_page"), \
                mock.patch.object(account_login, "site_cookies", side_effect=RuntimeError("closed")), \
                mock.patch.object(account_login, "browser_running", return_value=False):
            with self.assertRaises(RuntimeError):
                account_login.wait_for_session("muse2api")

    def test_cookies_of_other_sites_are_not_read_as_the_session(self):
        with mock.patch.object(account_login, "_cdp", return_value={"cookies": [
                {"name": "sso", "value": "1", "domain": ".grok.com"},
                {"name": "sso", "value": "2", "domain": "notgrok.com"},
                {"name": "x", "value": "3", "domain": "accounts.grok.com"}]}):
            self.assertEqual([c["value"] for c in account_login.site_cookies("grok.com")], ["1", "3"])

    def test_login_and_import_always_closes_the_login_window(self):
        with mock.patch.object(account_login, "wait_for_session", return_value=[{"name": "sso", "value": "v"}]), \
                mock.patch.object(account_login, "grok_import_sso", side_effect=account_login.PasswordNeeded()), \
                mock.patch.object(account_login, "close_browser") as close:
            with self.assertRaises(account_login.PasswordNeeded):
                account_login.login_and_import("grok2api")
        close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
