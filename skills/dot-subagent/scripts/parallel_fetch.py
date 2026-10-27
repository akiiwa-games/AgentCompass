"""Download many public URLs in parallel (standard library only), meant to be pasted into a packet and run on Dot's computer.

    from parallel_fetch import fetch_all
    results = fetch_all(urls, threads=16)       # {url: bytes, or None for 404}; raises if any other URL still fails

Measured 2026-10-10 on Dot's computer against data.binance.vision (24 monthly zips, 44 MB): one connection gets about
0.17 MB/s, so 1 thread took 260 s, 8 threads 65 s and 16 threads 40 s (every checksum matched). The limit is per connection,
not the machine (9 cores), so use 16 or more threads for downloads and keep CPU work (parsing) in a process pool of at most
8. A 404 is an answer (the file does not exist) and is returned as None; other errors are retried with a growing pause.
"""

from concurrent.futures import ThreadPoolExecutor
import time
import urllib.error
import urllib.request


def fetch_one(url, tries=5, timeout=120):
    """Bytes of the URL, None if it does not exist (404); other failures are retried, then raised."""
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 404:
                return None
            last = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        time.sleep(min(2 ** attempt, 20))
    raise last


def fetch_all(urls, threads=16, tries=5, timeout=120):
    """{url: bytes or None}, fetched with `threads` connections at once; the order of `urls` does not matter."""
    urls = list(dict.fromkeys(urls))
    with ThreadPoolExecutor(max(1, threads)) as pool:
        return dict(zip(urls, pool.map(lambda u: fetch_one(u, tries, timeout), urls)))
