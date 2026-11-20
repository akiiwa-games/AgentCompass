"""override_store.locked: one holder at a time, and a held store says it is busy whatever its lock file holds."""

import os
from pathlib import Path
import tempfile
import unittest

import override_store


def hold(handle):
    """Take the store's lock on an open handle of its lock file, as another process holding it would."""
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def release(handle):
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Locked(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'store.json'

    def test_a_second_holder_is_told_the_store_is_busy_and_enters_after_the_release(self):
        with override_store.locked(self.path):
            with self.assertRaises(TimeoutError):
                with override_store.locked(self.path, timeout=0.1):
                    self.fail('two holders at once')
        with override_store.locked(self.path, timeout=0.1):
            pass

    def test_a_store_held_before_its_lock_file_holds_a_byte_is_busy_not_denied(self):
        # Two first users of a store open its empty lock file together; the one that lost the race used to write
        # the first byte under the winner's lock and got "Permission denied" on Windows.
        for size in (0, 1):
            with self.subTest(lock_file_bytes=size):
                lock_file = Path(str(self.path) + '.lock')
                lock_file.write_bytes(b'\0' * size)
                with open(lock_file, 'a+b') as holder:
                    hold(holder)
                    try:
                        with self.assertRaises(TimeoutError):
                            with override_store.locked(self.path, timeout=0.1):
                                self.fail('entered a held store')
                    finally:
                        release(holder)
                with override_store.locked(self.path, timeout=0.1):
                    pass


if __name__ == '__main__':
    unittest.main()
