import http.server
import threading
import time
import unittest
import urllib.error

import parallel_fetch


class Handler(http.server.BaseHTTPRequestHandler):
    flaky = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        time.sleep(0.2)                       # a slow connection: each request costs 0.2 s
        if self.path == "/missing":
            self.send_error(404)
            return
        if self.path.startswith("/flaky"):
            seen = Handler.flaky.get(self.path, 0)
            Handler.flaky[self.path] = seen + 1
            if seen < 2:
                self.send_error(503)
                return
        body = self.path.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ParallelFetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_results_are_keyed_by_url_and_threads_make_it_faster(self):
        urls = [f"{self.base}/f{i}" for i in range(16)]
        start = time.time()
        one = parallel_fetch.fetch_all(urls, threads=1)
        serial = time.time() - start
        start = time.time()
        many = parallel_fetch.fetch_all(urls, threads=8)
        parallel = time.time() - start
        self.assertEqual(one, many)
        self.assertEqual(many[urls[3]], b"/f3")
        self.assertLess(parallel, serial / 3)

    def test_a_404_is_none_and_a_flaky_server_is_retried(self):
        original = parallel_fetch.time.sleep
        parallel_fetch.time.sleep = lambda seconds: None
        self.addCleanup(lambda: setattr(parallel_fetch.time, "sleep", original))
        got = parallel_fetch.fetch_all([f"{self.base}/missing", f"{self.base}/flaky1"], threads=2)
        self.assertIsNone(got[f"{self.base}/missing"])
        self.assertEqual(got[f"{self.base}/flaky1"], b"/flaky1")
        self.assertEqual(Handler.flaky["/flaky1"], 3)

    def test_a_url_that_keeps_failing_raises_after_the_tries(self):
        original = parallel_fetch.time.sleep
        parallel_fetch.time.sleep = lambda seconds: None
        self.addCleanup(lambda: setattr(parallel_fetch.time, "sleep", original))
        Handler.flaky["/flaky9"] = -100
        with self.assertRaises(urllib.error.HTTPError):
            parallel_fetch.fetch_all([f"{self.base}/flaky9"], threads=1, tries=3)

    def test_duplicate_urls_are_fetched_once(self):
        url = f"{self.base}/dup"
        self.assertEqual(parallel_fetch.fetch_all([url, url, url], threads=4), {url: b"/dup"})


if __name__ == "__main__":
    unittest.main()
