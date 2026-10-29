import json
import sys
import tempfile
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
    def test_login_opens_the_resolved_chrome_on_the_dedicated_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            chrome = Path(directory) / "chrome.exe"
            chrome.touch()
            profile = Path(directory) / "profile"
            with mock.patch.object(gateways, "chrome_path", return_value=chrome), \
                    mock.patch.object(account_login, "PROFILE", profile), \
                    mock.patch.object(account_login, "browser_running", side_effect=[False, True]), \
                    mock.patch.object(account_login.subprocess, "Popen") as launch:
                account_login.open_page("https://muse.ai/")
            args = launch.call_args.args[0]
            self.assertEqual(args[0], str(chrome))
            self.assertIn(f"--user-data-dir={profile}", args)
            self.assertIn("--remote-debugging-address=127.0.0.1", args)

    def test_waits_until_every_required_cookie_exists_then_returns_the_site_cookies(self):
        readings = iter([[], [{"name": "hatch_sess", "value": "", "domain": ".muse.ai"}],
                         [{"name": "hatch_sess", "value": "v", "domain": ".muse.ai"}],
                         [{"name": "hatch_sess", "value": "v", "domain": ".muse.ai"},
                          {"name": "hatch_native_auth_device", "value": "d", "domain": ".muse.ai"}]])
        with mock.patch.object(account_login, "open_page"), \
                mock.patch.object(account_login, "site_cookies", side_effect=lambda d: next(readings)), \
                mock.patch.object(account_login.time, "sleep"):
            cookies = account_login.wait_for_session("muse2api")
        self.assertEqual(cookies[0]["value"], "v")

    def test_closing_the_window_stops_waiting(self):
        with mock.patch.object(account_login, "open_page"), \
                mock.patch.object(account_login, "site_cookies", side_effect=RuntimeError("closed")), \
                mock.patch.object(account_login, "browser_running", return_value=False):
            with self.assertRaises(RuntimeError):
                account_login.wait_for_session("muse2api")

    def test_cookies_of_other_sites_are_not_read_as_the_session(self):
        with mock.patch.object(account_login, "_cdp", return_value={"cookies": [
                {"name": "hatch_sess", "value": "1", "domain": ".muse.ai"},
                {"name": "hatch_sess", "value": "2", "domain": "notmuse.ai"},
                {"name": "x", "value": "3", "domain": "accounts.muse.ai"}]}):
            self.assertEqual([c["value"] for c in account_login.site_cookies("muse.ai")], ["1", "3"])

    def test_login_and_import_always_closes_the_login_window(self):
        with mock.patch.object(account_login, "wait_for_session", return_value=[{"name": "hatch_sess", "value": "v"}]), \
                mock.patch.object(account_login, "muse_import", side_effect=RuntimeError("import failed")), \
                mock.patch.object(account_login, "close_browser") as close:
            with self.assertRaisesRegex(RuntimeError, "import failed"):
                account_login.login_and_import("muse2api")
        close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
